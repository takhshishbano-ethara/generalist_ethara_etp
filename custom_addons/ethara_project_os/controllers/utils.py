"""REST plumbing: authentication, role gating, uniform responses.

Two things this layer never does:

1. **Trust a client-supplied identity.** The acting employee is always resolved from
   the authenticated session or token. A request that says "employee_id: 42" is telling
   us who it wants to *act on*, and that is then checked against the caller's scope.
2. **Be the only line of defence.** Every scope check here has a matching ``ir.rule``
   behind it. If a controller forgets one, the ORM still refuses. This layer exists to
   return a helpful 403 instead of an ugly one.
"""

import functools
import json
import logging

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

ROLE_RANK = {'pm': 0, 'pl': 1, 'gpm': 2, 'admin': 3}


def respond(data=None, message='ok', status=200, errors=None):
    body = {'status_code': status, 'message': message, 'errors': errors or []}
    if data is not None:
        body['data'] = data
    return request.make_json_response(body, status=status)


class BadRequest(Exception):
    """A malformed request. Carries the message the client should see."""


def need_int(body, key, label=None):
    """A required integer parameter, or a 400 that says which one and why.

    Every one of these used to be a bare ``int(body['x'])``: a missing key raised
    KeyError and a non-numeric value raised ValueError, both of which surfaced as
    "500 Server error" — no signal to the caller, and a false alarm in error monitoring.
    """
    if key not in body or body[key] in (None, ''):
        raise BadRequest(f'{label or key} is required.')
    try:
        return int(body[key])
    except (TypeError, ValueError):
        raise BadRequest(f'{label or key} must be a number, got {body[key]!r}.') from None


def opt_int(body, key, default=None, minimum=None, maximum=None):
    """An optional integer, optionally bounded.

    The bounds matter for pagination: `limit=-5` is a perfectly valid integer that
    Postgres refuses, so without a floor it came back as an opaque 500 instead of a
    sentence naming the parameter.
    """
    if key not in body or body[key] in (None, ''):
        return default
    try:
        value = int(body[key])
    except (TypeError, ValueError):
        raise BadRequest(f'{key} must be a number, got {body[key]!r}.') from None
    if minimum is not None and value < minimum:
        raise BadRequest(f'{key} cannot be below {minimum}, got {value}.')
    if maximum is not None and value > maximum:
        value = maximum
    return value


def need_str(body, key, label=None):
    value = (body.get(key) or '').strip() if isinstance(body.get(key), str) else body.get(key)
    if not value:
        raise BadRequest(f'{label or key} is required.')
    return value


def _bearer_token():
    """The caller's token, whichever way they chose to send it.

    ``access-token`` is canonical (it is what the rest of this deployment uses), but the
    underscore spelling is dropped by most WSGI front ends — nginx and gunicorn both
    discard headers containing underscores by default — so a frontend developer who
    copies ``access_token`` from a doc gets a silent 401 with no clue why. Accepting the
    obvious variants, including ``Authorization: Bearer``, costs three lines and saves
    an afternoon.
    """
    headers = request.httprequest.headers
    for name in ('access-token', 'access_token', 'x-access-token'):
        token = headers.get(name)
        if token:
            return token.strip()
    authorization = headers.get('Authorization') or ''
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return None


def _authenticate():
    """Resolve the acting user.

    Accepts either a token header (the pattern the rest of this Odoo deployment uses,
    via api_auth_gateway) or an authenticated web session. Returns the user record,
    or None.
    """
    token = _bearer_token()
    if token:
        record = request.env['api.access_token'].sudo().search(
            [('access_token', '=', token)], order='id desc', limit=1)
        if not record or record.has_expired() or not record.user_id:
            return None
        request.update_env(user=record.user_id.id)
        return record.user_id
    user = request.env.user
    if not user or user._is_public():
        return None
    return user


def api_route(min_role='pm'):
    """Authenticate, resolve the Project OS role, and enforce a minimum level.

    The decorated method receives ``ctx``: the acting user, their employee record,
    their role and their visible-employee scope, all resolved once.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            user = _authenticate()
            if not user:
                return respond(message='Not signed in.', status=401)
            role = user._epo_role()
            if not role:
                return respond(
                    message='Your account has no Project OS role. Ask an Admin to grant '
                            'one.', status=403)
            if ROLE_RANK[role] < ROLE_RANK[min_role]:
                return respond(
                    message=f'This action needs the {min_role.upper()} role or higher.',
                    status=403)
            employee = user._epo_employee()
            if not employee and role != 'admin':
                return respond(
                    message='Your login is not linked to an employee record.', status=403)
            ctx = Ctx(user=user, employee=employee, role=role)
            try:
                return func(self, ctx, *args, **kwargs)
            except BadRequest as exc:
                return respond(message=str(exc), status=400, errors=[str(exc)])
            except (ValueError, TypeError, KeyError) as exc:
                # A malformed request is the caller's problem, not a server fault.
                # Without this every stray query string produced a 500, which tells the
                # client nothing and buries real faults in the monitoring noise.
                _logger.info('Project OS: malformed request on %s: %s',
                             request.httprequest.path, exc)
                return respond(message='Malformed request.', status=400,
                               errors=[str(exc)])
            except AccessError:
                # Must come first: AccessError subclasses UserError, so catching
                # UserError above would turn every permission failure into a 400.
                # Odoo's own message is written for a backend user ("perhaps you can
                # win over your friendly administrator with cookies") — an API client
                # gets the fact instead.
                return respond(
                    message='You do not have access to that record.', status=403)
            except (UserError, ValidationError) as exc:
                return respond(message=str(exc), status=400, errors=[str(exc)])
            except Exception as exc:  # noqa: BLE001
                _logger.exception('Project OS API error on %s', request.httprequest.path)
                return respond(message='Server error.', status=500, errors=[str(exc)])
        return wrapper
    return decorator


class Ctx:
    """Everything a handler needs about the caller, resolved once per request."""

    def __init__(self, user, employee, role):
        self.user = user
        self.employee = employee
        self.role = role

    @property
    def env(self):
        return request.env

    @property
    def is_manager(self):
        return self.role in ('gpm', 'admin')

    @property
    def scope_employee_ids(self):
        if not hasattr(self, '_scope'):
            self._scope = self.user._epo_scope_employee_ids()
        return self._scope

    @property
    def accessible_project_ids(self):
        if not hasattr(self, '_projects'):
            self._projects = self.user._epo_accessible_project_ids()
        return self._projects

    def assert_can_see_employee(self, employee_id):
        if int(employee_id) not in self.scope_employee_ids:
            raise AccessError('That person is outside your scope.')

    def assert_can_see_project(self, project_id):
        if not self.is_manager and int(project_id) not in self.accessible_project_ids:
            raise AccessError('You have no access to that project.')

    def today(self):
        return fields.Date.context_today(self.env.user)


def payload():
    """Request body as a dict, whatever the client sent it as."""
    if request.httprequest.is_json:
        return request.get_json_data() or {}
    try:
        raw = request.httprequest.get_data(as_text=True)
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return dict(request.httprequest.form or {})


def route(path, methods, min_role='pm'):
    """Shorthand for the JSON routes this module exposes.

    ``auth='public'`` with our own check inside is deliberate: it lets one endpoint
    serve both a token client (the SPA, or a mobile app) and a session client without
    two route registrations.

    ``cors='*'`` matches the rest of this deployment (see api_auth_gateway) and is what
    lets a frontend served from its own origin call this API at all. Note the pairing:
    a wildcard CORS origin cannot be combined with cookie credentials, so a
    cross-origin frontend must authenticate with the ``access_token`` header rather
    than a session cookie. Same-origin deployments can use either.

    ``OPTIONS`` is added to every route so the browser preflight succeeds; Odoo answers
    it from the routing layer before the handler runs.
    """
    verbs = list(methods)
    if 'OPTIONS' not in verbs:
        verbs.append('OPTIONS')

    def decorator(func):
        wrapped = api_route(min_role=min_role)(func)
        return http.route(
            path, type='http', auth='public', methods=verbs, csrf=False,
            cors='*', save_session=False)(wrapped)
    return decorator
