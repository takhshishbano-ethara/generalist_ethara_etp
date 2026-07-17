"""Ethara Project Budget REST API.

Standalone HTTP endpoints that back the Flutter "Create Budget" flow. Every
route lives under `/api/v1/ethara_project/*` and is wired exclusively to the
`ethara.project.*` model family - this module has ZERO dependency on the
`etp_projects` addon.

Auth: every endpoint is decorated with `@validate_token`. Flutter must send
the caller's access token in the `access-token` HTTP header. The endpoint URL
must also be registered in `data/api_endpoint_data.xml` and attached to the
caller's role via the standard `api_role_endpoint` junction.

Response envelope: all endpoints return the shared api_auth_gateway shape
`{"message": str, "errors": [], "data": {...}, "status_code": int}` via
`return_Response`. Flutter should parse `body.data.<field>`.

Endpoints implemented:
- GET  /api/v1/ethara_project/projects/active_list
- GET  /api/v1/ethara_project/budget/default_approvers
- GET  /api/v1/ethara_project/budget/approvers/search
- GET  /api/v1/ethara_project/budget/models
- GET  /api/v1/ethara_project/budget/subscriptions
- GET  /api/v1/ethara_project/budget/infra_types
- POST /api/v1/ethara_project/budget/create
"""
import base64
import json
import logging

from odoo import http, fields
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_MODEL = 'ethara.project'
BUDGET_MODEL = 'ethara.project.budget'
AI_MODEL = 'ethara.project.ai.model'
INFRA_MODEL = 'ethara.project.infra.type'
SUBSCRIPTION_MODEL = 'ethara.project.subscription'

VALID_BUDGET_TYPES = ('rnd', 'operations')
VALID_PRIORITIES = ('low', 'normal', 'high', 'urgent')
VALID_COST_TYPES = ('per_task', 'per_trajectory')

CONFIG_PARAM_DEFAULT_APPROVERS = 'ethara_project.default_approver_user_ids'


# ---------------------------------------------------------------------------
# Helpers (all internal - not exposed via HTTP)
# ---------------------------------------------------------------------------

def _coerce_int(value, default=None):
    try:
        if value is None or value == '':
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default=0.0):
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_date(value):
    if not value:
        return False
    if isinstance(value, str):
        try:
            return fields.Date.from_string(value)
        except Exception:
            return False
    return value


def _pagination(params, default_limit=100, max_limit=500):
    limit = _coerce_int(params.get('limit'), default_limit) or default_limit
    offset = _coerce_int(params.get('offset'), 0) or 0
    if limit <= 0:
        return None, None, return_Response(
            message='limit must be positive.', status=400, data={},
        )
    if limit > max_limit:
        limit = max_limit
    if offset < 0:
        return None, None, return_Response(
            message='offset must be >= 0.', status=400, data={},
        )
    return limit, offset, None


def _read_multipart_or_json():
    """Return (parsed_json_dict, list_of_uploaded_files).

    Flutter may POST either application/json or multipart/form-data. Multipart
    payloads are expected to include a `payload` field holding the JSON body
    and an `attachments` field with any file uploads.
    """
    content_type = (request.httprequest.content_type or '').lower()
    if content_type.startswith('multipart/form-data'):
        form = request.httprequest.form
        files = request.httprequest.files.getlist('attachments') or []
        raw = form.get('payload') or '{}'
        try:
            jdata = json.loads(raw) if raw else {}
        except ValueError:
            jdata = {}
        return jdata or {}, files
    try:
        raw = request.httprequest.get_data(as_text=True) or ''
        jdata = json.loads(raw) if raw else {}
    except ValueError:
        jdata = {}
    return jdata or {}, []


def _missing_ids(model, ids):
    if not ids:
        return []
    found = request.env[model].sudo().browse(list(ids)).exists().ids
    return [i for i in ids if i not in found]


def _get_default_approver_user_ids(env):
    """Return the configured default project-budget approver user ids.

    Reads a comma-separated integer list stored on
    `ir.config_parameter` under key `ethara_project.default_approver_user_ids`.
    Admins can seed this manually via Settings > Technical > System Parameters.
    Returns an empty list when unset - callers must still be able to supply
    `approver_ids` in the payload.
    """
    raw = env['ir.config_parameter'].sudo().get_param(
        CONFIG_PARAM_DEFAULT_APPROVERS, '',
    )
    ids = []
    for token in (raw or '').split(','):
        token = token.strip()
        if token.isdigit():
            ids.append(int(token))
    if not ids:
        return []
    existing = env['res.users'].sudo().browse(ids).exists()
    return existing.ids


def _serialize_project(p):
    return {
        'id': p.id,
        'project_name': p.name or '',
        'client': p.client_name or '',
        'internal_project_name': p.internal_project_name or '',
        'project_goal': p.project_goal or '',
        'start_date': p.start_date.isoformat() if p.start_date else None,
        'end_date': p.end_date.isoformat() if p.end_date else None,
        'state': p.state or '',
        'status': p.state or '',
    }


def _serialize_user(u):
    return {
        'id': u.id,
        'name': u.name or '',
        'login': u.login or '',
        'email': u.email or (u.partner_id.email if u.partner_id else '') or '',
    }


def _parse_model_entry(entry, idx, allow_zero_cost=False):
    if not isinstance(entry, dict):
        return None, 0.0, f'models[{idx}] must be an object.'
    model_id = _coerce_int(entry.get('model_id') or entry.get('ai_model_id'))
    if not model_id:
        return None, 0.0, f'models[{idx}].model_id is required.'
    cost_type = entry.get('cost_type') or 'per_task'
    if cost_type not in VALID_COST_TYPES:
        return None, 0.0, (
            f'models[{idx}].cost_type must be one of {list(VALID_COST_TYPES)}.'
        )
    per_task_cost = _coerce_float(entry.get('per_task_cost'), 0.0)
    per_trajectory_cost = _coerce_float(entry.get('per_trajectory_cost'), 0.0)
    iterations = _coerce_int(
        entry.get('no_of_trajectory') or entry.get('iterations'), 0,
    ) or 0
    if cost_type == 'per_trajectory':
        if not allow_zero_cost and (per_trajectory_cost <= 0.0 or iterations <= 0):
            return None, 0.0, (
                f'models[{idx}] requires positive per_trajectory_cost and '
                f'no_of_trajectory when cost_type=per_trajectory.'
            )
        per_task_cost = per_trajectory_cost * iterations
    else:
        if not allow_zero_cost and per_task_cost <= 0.0:
            return None, 0.0, (
                f'models[{idx}].per_task_cost must be > 0 '
                f'when cost_type=per_task.'
            )
    vals = {
        'ai_model_id': model_id,
        'ai_model_name': (entry.get('ai_model_name') or '').strip() or False,
        'cost_type': cost_type,
        'per_task_cost': per_task_cost,
        'per_trajectory_cost': per_trajectory_cost,
        'iterations': iterations,
    }
    return vals, per_task_cost, None


def _parse_infra_entry(entry, idx):
    if not isinstance(entry, dict):
        return None, 0.0, f'infra[{idx}] must be an object.'
    infra_id = _coerce_int(entry.get('infra_id') or entry.get('infra_type_id'))
    if not infra_id:
        return None, 0.0, f'infra[{idx}].infra_id is required.'
    cost = _coerce_float(entry.get('cost') or entry.get('budget_amount'), 0.0)
    if cost < 0.0:
        return None, 0.0, f'infra[{idx}].cost must be >= 0.'
    vals = {
        'infra_type_id': infra_id,
        'budget_amount': cost,
        'description': (entry.get('description') or '').strip() or False,
    }
    for src, dst in (
        ('instance_type', 'instance_type'),
        ('price_unit', 'price_unit'),
        ('volume_type', 'volume_type'),
    ):
        v = (entry.get(src) or '').strip()
        if v:
            vals[dst] = v
    for src, dst in (
        ('unit_price_usd', 'unit_price_usd'),
        ('quantity', 'quantity'),
        ('duration_hours', 'duration_hours'),
        ('ebs_storage_gb', 'ebs_storage_gb'),
        ('volume_rate_usd_per_gb_mo', 'volume_rate_usd_per_gb_mo'),
    ):
        v = _coerce_float(entry.get(src), 0.0)
        if v:
            vals[dst] = v
    return vals, cost, None


def _parse_subscription_entry(entry, idx):
    if not isinstance(entry, dict):
        return None, 0.0, f'subscription[{idx}] must be an object.'
    sub_id = _coerce_int(entry.get('subscription_id'))
    if not sub_id:
        return None, 0.0, f'subscription[{idx}].subscription_id is required.'
    assigned_to = entry.get('assigned_to') or entry.get('assigned_user_ids') or []
    if not isinstance(assigned_to, (list, tuple)):
        return None, 0.0, (
            f'subscription[{idx}].assigned_to must be a list of user ids.'
        )
    assigned_ids = [x for x in (_coerce_int(uid) for uid in assigned_to) if x]
    vals = {
        'subscription_id': sub_id,
        'assigned_user_ids': [(6, 0, assigned_ids)],
    }
    per_seat = _coerce_float(entry.get('cost'), 0.0)
    monthly_total = per_seat * len(assigned_ids)
    return vals, monthly_total, None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class EtharaBudgetController(http.Controller):
    """REST endpoints for the Flutter "Create Budget" wizard."""

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/projects/active_list
    #
    # Replacement for the legacy `/api/v2/get_active_project_list`.
    # Returns the list of active `ethara.project` records the caller can pick
    # in the "Project" dropdown of the Create Budget page.
    #
    # Query params (all optional):
    #   - search: fuzzy match against name / client_name
    #   - limit / offset: standard pagination (defaults: 100 / 0, max 500)
    #
    # Response shape:
    #   {message, errors, status_code, data: {
    #       total: int, limit: int, offset: int,
    #       record: [{id, project_name, client, internal_project_name,
    #                 project_goal, start_date, end_date, state, status}]
    #   }}
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/projects/active_list',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_active_projects(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            domain = [('state', 'in', ('start', 'pause'))]
            search = (params.get('search') or '').strip()
            if search:
                domain += [
                    '|', '|',
                    ('name', 'ilike', search),
                    ('client_name', 'ilike', search),
                    ('internal_project_name', 'ilike', search),
                ]
            Project = request.env[PROJECT_MODEL].sudo()
            total = Project.search_count(domain)
            records = Project.search(
                domain, limit=limit, offset=offset, order='id desc',
            )
            items = [_serialize_project(p) for p in records]
            return return_Response(
                message='Active projects fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'record': items,
                    'total_record_count': total,
                    'count': len(items),
                },
            )
        except Exception as e:
            _logger.exception('ethara_project projects/active_list failed')
            return return_Response(
                message='Failed to list active projects.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/default_approvers
    #
    # Returns users configured as default approvers. Admins seed the list via
    # `ir.config_parameter` key `ethara_project.default_approver_user_ids`
    # (comma-separated res.users ids). Flutter pre-populates the approver
    # picker with this list; users can then add/remove and submit as
    # `approver_ids` in POST /budget/create.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/default_approvers',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_default_approvers(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            user_ids = _get_default_approver_user_ids(request.env)
            users = request.env['res.users'].sudo().browse(user_ids).exists()
            search = (params.get('search') or '').strip().lower()
            if search:
                users = users.filtered(
                    lambda u: search in (u.name or '').lower()
                    or search in (u.login or '').lower()
                    or search in (u.email or '').lower()
                )
            total = len(users)
            page = users[offset:offset + limit]
            return return_Response(
                message='Default approvers fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'approvers': [_serialize_user(u) for u in page],
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/default_approvers failed')
            return return_Response(
                message='Failed to list default approvers.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/approvers/search
    #
    # Free-text search across all internal res.users to add extra approvers
    # beyond the defaults. Flutter uses this in the "Add approver" autocomplete.
    #
    # Query params:
    #   - search: name/login/email substring (required for a non-empty result)
    #   - limit / offset: pagination (defaults 20 / 0, max 100)
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/approvers/search',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def search_approvers(self, **params):
        try:
            limit, offset, err = _pagination(
                params, default_limit=20, max_limit=100,
            )
            if err:
                return err
            search = (params.get('search') or '').strip()
            domain = [('share', '=', False), ('active', '=', True)]
            if search:
                domain += [
                    '|', '|',
                    ('name', 'ilike', search),
                    ('login', 'ilike', search),
                    ('email', 'ilike', search),
                ]
            Users = request.env['res.users'].sudo()
            total = Users.search_count(domain)
            records = Users.search(
                domain, limit=limit, offset=offset, order='name',
            )
            return return_Response(
                message='Approvers fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'approvers': [_serialize_user(u) for u in records],
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/approvers/search failed')
            return return_Response(
                message='Failed to search approvers.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/models
    #
    # Returns the AI Model provider catalog (rows in `ethara.project.ai.model`)
    # used to populate the Provider dropdown on the "AI Model Lines" tab of the
    # Create Budget wizard. To fetch the actual models available under each
    # provider (using the provider's api_key + api_url) call the separate
    # `/api/v1/ethara_project/ai_models/list?provider=<name>` endpoint.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/models',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_ai_models_catalog(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            domain = []
            if not _coerce_int(params.get('include_inactive'), 0):
                domain.append(('active', '=', True))
            search = (params.get('search') or '').strip()
            if search:
                domain += [
                    '|',
                    ('name', 'ilike', search),
                    ('provider', 'ilike', search),
                ]
            Model = request.env[AI_MODEL].sudo()
            total = Model.search_count(domain)
            records = Model.search(
                domain, limit=limit, offset=offset, order='sequence, name',
            )
            items = [
                {
                    'id': r.id,
                    'name': r.name,
                    'provider': r.provider or '',
                    'active': r.active,
                }
                for r in records
            ]
            return return_Response(
                message='AI models fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'models': items,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/models failed')
            return return_Response(
                message='Failed to list AI models.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/subscriptions
    #
    # Returns the subscription catalog (`ethara.project.subscription`) used
    # to populate the Subscription dropdown on the "Subscription Lines" tab.
    # `per_day_cost` is derived server-side as `cost / 30`.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/subscriptions',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_subscriptions(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            domain = []
            if not _coerce_int(params.get('include_inactive'), 0):
                domain.append(('active', '=', True))
            search = (params.get('search') or '').strip()
            if search:
                domain.append(('name', 'ilike', search))
            Model = request.env[SUBSCRIPTION_MODEL].sudo()
            total = Model.search_count(domain)
            records = Model.search(
                domain, limit=limit, offset=offset, order='sequence, name',
            )
            items = [
                {
                    'id': r.id,
                    'name': r.name,
                    'cost': r.cost or 0.0,
                    'per_day_cost': (r.cost or 0.0) / 30.0,
                    'active': r.active,
                }
                for r in records
            ]
            return return_Response(
                message='Subscriptions fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'subscriptions': items,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/subscriptions failed')
            return return_Response(
                message='Failed to list subscriptions.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra_types
    #
    # Returns the infrastructure type catalog (`ethara.project.infra.type`)
    # used to populate the Infrastructure dropdown on the "Infrastructure
    # Lines" tab. The catalog is auto-populated by the AWS Pricing Sync cron
    # or the "Sync AWS Pricing Now" button in Settings.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra_types',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_infra_types(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            domain = []
            if not _coerce_int(params.get('include_inactive'), 0):
                domain.append(('active', '=', True))
            search = (params.get('search') or '').strip()
            if search:
                domain += [
                    '|',
                    ('name', 'ilike', search),
                    ('code', 'ilike', search),
                ]
            aws_only = _coerce_int(params.get('aws_only'), 0)
            if aws_only:
                domain.append(('is_aws_managed', '=', True))
            Model = request.env[INFRA_MODEL].sudo()
            total = Model.search_count(domain)
            records = Model.search(
                domain, limit=limit, offset=offset, order='sequence, name',
            )
            items = [
                {
                    'id': r.id,
                    'name': r.name,
                    'code': r.code or '',
                    'aws_service_code': r.aws_service_code or '',
                    'is_aws_managed': bool(r.is_aws_managed),
                    'active': r.active,
                }
                for r in records
            ]
            return return_Response(
                message='Infrastructure types fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'infra': items,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra_types failed')
            return return_Response(
                message='Failed to list infrastructure types.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # POST /api/v1/ethara_project/budget/create
    #
    # Creates a new `ethara.project.budget` record with nested lines. Accepts
    # both application/json and multipart/form-data (multipart adds file
    # uploads under the `attachments` field; the JSON payload goes in a form
    # field named `payload`).
    #
    # Required JSON payload keys:
    #   - project_id (int)               -> ethara.project id
    #   - budget_type ("rnd"|"operations")
    #   - total_no_of_tasks (int > 0)
    #
    # Optional payload keys:
    #   - budget_amount (float; required > 0 when budget_type = "rnd")
    #   - description (str)
    #   - buffer_pct (float, default 0)
    #   - priority ("low"|"normal"|"high"|"urgent", default "normal")
    #   - approver_ids ([int])           -> merged with default approvers
    #   - models: [{model_id, cost_type, per_task_cost | per_trajectory_cost,
    #               no_of_trajectory, ai_model_name}]
    #   - infra:  [{infra_id, cost, description, instance_type, quantity,
    #               duration_hours, unit_price_usd, ebs_storage_gb, ...}]
    #   - subscription: [{subscription_id, cost, assigned_to: [user_id]}]
    #
    # Response: full budget dict envelope
    #   {message, status_code, data: {id, name, project_id, budget_type,
    #    state, budget_amount, total_tasks, approver_ids, ...}}
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/create',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def create_budget(self, **params):
        try:
            jdata, uploaded_files = _read_multipart_or_json()

            project_id = _coerce_int(jdata.get('project_id'))
            if not project_id:
                return return_Response(
                    message='project_id is required.',
                    status=400, data={},
                )
            project = request.env[PROJECT_MODEL].sudo().browse(project_id).exists()
            if not project:
                return return_Response(
                    message=f'Project {project_id} does not exist.',
                    status=400, data={},
                )

            budget_type = (jdata.get('budget_type') or '').strip()
            if budget_type not in VALID_BUDGET_TYPES:
                return return_Response(
                    message=(
                        f'budget_type must be one of {list(VALID_BUDGET_TYPES)}.'
                    ),
                    status=400, data={},
                )
            is_rnd = (budget_type == 'rnd')

            initial_budget = _coerce_float(jdata.get('budget_amount'), 0.0)
            if is_rnd and initial_budget <= 0.0:
                return return_Response(
                    message=(
                        'budget_amount is required and must be > 0 for R&D budgets.'
                    ),
                    status=400, data={},
                )

            total_tasks = _coerce_int(
                jdata.get('total_no_of_tasks') or jdata.get('total_tasks'), 0,
            ) or 0
            if total_tasks <= 0:
                return return_Response(
                    message='total_no_of_tasks must be > 0.',
                    status=400, data={},
                )

            description = (jdata.get('description') or '').strip()
            buffer_pct = _coerce_float(jdata.get('buffer_pct'), 0.0)

            priority = (jdata.get('priority') or 'normal').strip()
            if priority not in VALID_PRIORITIES:
                return return_Response(
                    message=(
                        f'priority must be one of {list(VALID_PRIORITIES)}.'
                    ),
                    status=400, data={},
                )

            dup = request.env[BUDGET_MODEL].sudo().search([
                ('ethara_project_id', '=', project_id),
                ('project_type', '=', budget_type),
            ], limit=1)
            if dup:
                return return_Response(
                    message=(
                        f'A {budget_type} budget already exists for project '
                        f'{project.name!r}.'
                    ),
                    status=400, data={},
                )

            raw_approver_ids = jdata.get('approver_ids') or []
            if not isinstance(raw_approver_ids, (list, tuple)):
                return return_Response(
                    message='approver_ids must be a list of user ids.',
                    status=400, data={},
                )
            payload_approver_ids = [
                x for x in (_coerce_int(v) for v in raw_approver_ids) if x
            ]
            default_ids = _get_default_approver_user_ids(request.env)
            approver_ids = list(
                dict.fromkeys(default_ids + payload_approver_ids)
            )
            if not approver_ids:
                return return_Response(
                    message=(
                        'No approvers resolved. Provide approver_ids or seed '
                        'the ir.config_parameter '
                        f'{CONFIG_PARAM_DEFAULT_APPROVERS!r}.'
                    ),
                    status=400, data={},
                )
            missing_approvers = _missing_ids('res.users', approver_ids)
            if missing_approvers:
                return return_Response(
                    message=(
                        f'Approver user ids do not exist: {missing_approvers}.'
                    ),
                    status=400, data={},
                )

            raw_models = jdata.get('models') or []
            if not isinstance(raw_models, list):
                return return_Response(
                    message='models must be a list.', status=400, data={},
                )
            model_cmds = []
            for idx, entry in enumerate(raw_models):
                vals, _cost, err = _parse_model_entry(
                    entry, idx, allow_zero_cost=is_rnd,
                )
                if err:
                    return return_Response(message=err, status=400, data={})
                model_cmds.append((0, 0, vals))
            if model_cmds:
                missing_models = _missing_ids(
                    AI_MODEL, [c[2]['ai_model_id'] for c in model_cmds],
                )
                if missing_models:
                    return return_Response(
                        message=f'AI model ids do not exist: {missing_models}.',
                        status=400, data={},
                    )

            raw_infra = jdata.get('infra') or []
            if not isinstance(raw_infra, list):
                return return_Response(
                    message='infra must be a list.', status=400, data={},
                )
            infra_cmds = []
            for idx, entry in enumerate(raw_infra):
                vals, _cost, err = _parse_infra_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                infra_cmds.append((0, 0, vals))
            if infra_cmds:
                missing_infra = _missing_ids(
                    INFRA_MODEL, [c[2]['infra_type_id'] for c in infra_cmds],
                )
                if missing_infra:
                    return return_Response(
                        message=(
                            f'Infrastructure type ids do not exist: '
                            f'{missing_infra}.'
                        ),
                        status=400, data={},
                    )

            raw_subs = (
                jdata.get('subscription') or jdata.get('subscriptions') or []
            )
            if not isinstance(raw_subs, list):
                return return_Response(
                    message='subscription must be a list.',
                    status=400, data={},
                )
            sub_cmds = []
            for idx, entry in enumerate(raw_subs):
                vals, _monthly, err = _parse_subscription_entry(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                sub_cmds.append((0, 0, vals))
            if sub_cmds:
                missing_subs = _missing_ids(
                    SUBSCRIPTION_MODEL,
                    [c[2]['subscription_id'] for c in sub_cmds],
                )
                if missing_subs:
                    return return_Response(
                        message=(
                            f'Subscription ids do not exist: {missing_subs}.'
                        ),
                        status=400, data={},
                    )

            vals = {
                'ethara_project_id': project_id,
                'project_type': budget_type,
                'budget_amount': initial_budget,
                'total_tasks': total_tasks,
                'buffer_pct': buffer_pct,
                'priority': priority,
                'description': description,
                'approver_user_ids': [(6, 0, approver_ids)],
            }
            if model_cmds:
                vals['model_line_ids'] = model_cmds
            if infra_cmds:
                vals['infra_line_ids'] = infra_cmds
            if sub_cmds:
                vals['subscription_line_ids'] = sub_cmds

            budget = request.env[BUDGET_MODEL].sudo().create(vals)

            attachment_urls = []
            for f in uploaded_files or []:
                try:
                    data = f.read()
                except Exception:
                    continue
                if not data:
                    continue
                b64 = base64.b64encode(data).decode('utf-8')
                att = request.env['ir.attachment'].sudo().create({
                    'name': getattr(f, 'filename', '') or 'attachment.bin',
                    'type': 'binary',
                    'datas': b64,
                    'res_model': BUDGET_MODEL,
                    'res_id': budget.id,
                    'public': False,
                })
                attachment_urls.append(f'/web/content/{att.id}?download=1')

            return return_Response(
                message='Ethara project budget created.', status=200,
                data={'data': {
                    'id': budget.id,
                    'name': budget.name,
                    'project_id': project.id,
                    'project_name': project.name or '',
                    'budget_type': budget.project_type,
                    'state': budget.state,
                    'budget_amount': budget.budget_amount or 0.0,
                    'total_tasks': budget.total_tasks or 0,
                    'buffer_pct': budget.buffer_pct or 0.0,
                    'priority': budget.priority or '',
                    'description': budget.description or '',
                    'approver_ids': budget.approver_user_ids.ids,
                    'model_line_ids': budget.model_line_ids.ids,
                    'infra_line_ids': budget.infra_line_ids.ids,
                    'subscription_line_ids': budget.subscription_line_ids.ids,
                    'attachment_urls': attachment_urls,
                }},
            )
        except Exception as e:
            _logger.exception('ethara_project budget/create failed')
            return return_Response(
                message='Failed to create Ethara project budget.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # LEGACY URL ALIASES for the pre-existing Flutter build.
    #
    # Rationale: the Flutter web bundle currently in production still points
    # at the old URLs owned by `task_forge_bridge` (/api/v2/*) and
    # `etp_projects` (/api/v1/etp_projects/budget/*). Both modules are
    # uninstalled on the Ethara Odoo, so those URLs 404. Rather than block
    # QA on a Flutter rebuild, ethara_project re-registers the exact same
    # URL patterns and delegates to the canonical handlers above.
    #
    # These aliases are intentionally thin passthroughs. Delete this block
    # once Flutter's ApiConstants is updated to the canonical URLs.
    # -----------------------------------------------------------------------

    @http.route(
        '/api/v2/get_active_project_list',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_get_active_project_list(self, **params):
        return self.list_active_projects(**params)

    @http.route(
        '/api/v1/etp_projects/budget/default_approvers',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_default_approvers(self, **params):
        return self.list_default_approvers(**params)

    @http.route(
        '/api/v1/etp_projects/budget/models',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_budget_models(self, **params):
        return self.list_ai_models_catalog(**params)

    @http.route(
        '/api/v1/etp_projects/budget/subscriptions',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_budget_subscriptions(self, **params):
        return self.list_subscriptions(**params)

    @http.route(
        '/api/v1/etp_projects/budget/infra',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_budget_infra(self, **params):
        return self.list_infra_types(**params)

    @http.route(
        '/api/v1/etp_projects/budget/create',
        methods=['POST'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def alias_budget_create(self, **params):
        return self.create_budget(**params)

