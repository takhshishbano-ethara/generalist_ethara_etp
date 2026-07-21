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
import threading

from odoo import api, http, fields
from odoo.modules.registry import Registry
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.ethara_project.services import aws_pricing_service

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_MODEL = 'ethara.project'
BUDGET_MODEL = 'ethara.project.budget'
AI_MODEL = 'ethara.project.ai.model'
INFRA_MODEL = 'ethara.project.infra.type'
INFRA_PROVIDER_MODEL = 'ethara.project.infra.provider'
SUBSCRIPTION_MODEL = 'ethara.project.subscription'

VALID_BUDGET_TYPES = ('rnd', 'operations')
VALID_PRIORITIES = ('low', 'normal', 'high', 'urgent')
# R&D budgets are split into sub-types; uniqueness is scoped per sub-type so a
# project can hold one budget per (type, sub-type) — e.g. rnd/testing AND
# rnd/sampling side by side. Only meaningful when budget_type == 'rnd'.
VALID_RND_SUB_TYPES = ('testing', 'sampling')
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


def _coerce_bool(value, default=False):
    """Coerce a JSON/multipart value to a bool. Multipart form values arrive as
    strings, so treat 'true'/'1'/'yes'/'on' (any case) as True."""
    if isinstance(value, bool):
        return value
    if value is None or value == '':
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


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
            # Filter to a single provider (used by the manual infra picker to
            # list a non-AWS provider's services). Accepts either the numeric
            # provider id or the provider code (e.g. 'microsoft').
            provider_id = _coerce_int(params.get('provider_id'), 0)
            if provider_id:
                domain.append(('provider_id', '=', provider_id))
            provider_code = (params.get('provider_code') or '').strip().lower()
            if provider_code:
                domain.append(('provider_id.code', '=', provider_code))
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
                    'provider_id': r.provider_id.id or None,
                    'provider_code': r.provider_id.code or '',
                    'pricing_source': (
                        r.provider_id.pricing_source or ''
                    ),
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
    # GET /api/v1/ethara_project/budget/infra/providers
    #
    # Returns the cloud infrastructure provider catalog
    # (`ethara.project.infra.provider`) used to populate the Provider selector
    # on the Infrastructure step. Providers are managed from the Odoo backend
    # (Ethara Projects -> AWS Budgets -> Infrastructure Providers). Each row's
    # `pricing_source` tells the client which pricing UI to show: `aws_live`
    # for the live AWS configurator, `manual` for a plain unit-price form.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/providers',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_infra_providers(self, **params):
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
            Model = request.env[INFRA_PROVIDER_MODEL].sudo()
            total = Model.search_count(domain)
            records = Model.search(
                domain, limit=limit, offset=offset, order='sequence, name',
            )
            items = [
                {
                    'id': r.id,
                    'name': r.name,
                    'code': r.code or '',
                    'pricing_source': r.pricing_source or '',
                    'active': r.active,
                }
                for r in records
            ]
            return return_Response(
                message='Infrastructure providers fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'providers': items,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/providers failed')
            return return_Response(
                message='Failed to list infrastructure providers.',
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
    #    state, budget_amount, total_tasks, approver_ids, ..., phase_ids,
    #    request_id}}
    # -----------------------------------------------------------------------
    @staticmethod
    def _defer_background(env, func):
        """Run `func(new_env)` in a daemon thread after the current tx commits.

        Ported from etp_projects' project_budget_controller._defer_background.
        Enables moving heavy post-create work off the HTTP request cycle so
        the client's spinner returns fast.
        """
        dbname = env.cr.dbname
        uid = env.uid
        ctx = dict(env.context)

        def _launch():
            def _run():
                try:
                    registry = Registry(dbname)
                    with registry.cursor() as new_cr:
                        new_env = api.Environment(new_cr, uid, ctx)
                        try:
                            func(new_env)
                            new_cr.commit()
                        except Exception:
                            new_cr.rollback()
                            _logger.exception(
                                'Deferred background job failed'
                            )
                except Exception:
                    _logger.exception(
                        'Deferred background job setup failed'
                    )
            threading.Thread(target=_run, daemon=True).start()

        env.cr.postcommit.add(_launch)

    @staticmethod
    def _create_phases_and_submit_request(env, budget_id, jdata):
        """Create phase(s) and auto-submit first request. Callable from bg thread.

        Refactored to accept `env` parameter (instead of using `request.env`)
        so it can run in a fresh cursor from a deferred background thread
        via `_defer_background`. This moves ~19s of phase-creation work off
        the HTTP request cycle.

        For each `batches[]` entry an `ethara.project.phase` is created and the
        budget's model/infra/subscription lines are cloned onto it. The FIRST
        phase additionally gets an `ethara.project.phase.request` that is
        immediately pushed through `action_submit_for_approval()` so it appears
        on the CTO -> CFO approvals queue. The requester is the current token
        user (TPM/R&D/etc. who raised the budget).

        Every step is wrapped in its own `cr.savepoint()` so a failure here
        never rolls back the already-committed budget. Failures are logged via
        `_logger.exception(...)` and swallowed. If `action_submit_for_approval`
        cannot run (e.g. the raiser lacks the PL/TPM role, or the budget has no
        approvers) the request is simply left in draft.

        Returns (list_of_phase_ids, first_request_id_or_None) - primarily for
        logging in the bg thread context (caller doesn't use the return values
        when invoked via _defer_background).
        """
        PHASE_MODEL = 'ethara.project.phase'
        REQUEST_MODEL = 'ethara.project.phase.request'
        BUDGET_MODEL_LOCAL = 'ethara.project.budget'

        budget = env[BUDGET_MODEL_LOCAL].sudo().browse(budget_id).exists()
        if not budget:
            _logger.warning(
                'Deferred phase creation: budget %s no longer exists.',
                budget_id,
            )
            return [], None

        raw_batches = jdata.get('batches') or []
        if not isinstance(raw_batches, list) or not raw_batches:
            # No batches -> behave exactly as before (budget only).
            return [], None

        # Parse batch specs. A bad entry is skipped (logged) rather than
        # aborting the whole budget.
        budget_buffer = budget.buffer_pct or 0.0
        specs = []
        for idx, entry in enumerate(raw_batches):
            if not isinstance(entry, dict):
                _logger.warning('batches[%s] is not an object; skipped.', idx)
                continue
            no_of_task = _coerce_int(
                entry.get('no_of_task')
                if entry.get('no_of_task') is not None
                else entry.get('total_tasks'),
                0,
            ) or 0
            start_date = _coerce_date(entry.get('start_date'))
            end_date = _coerce_date(entry.get('end_date'))
            if no_of_task <= 0 or not start_date or not end_date:
                _logger.warning(
                    'batches[%s] missing no_of_task/start_date/end_date; '
                    'skipped.', idx,
                )
                continue
            if end_date < start_date:
                _logger.warning(
                    'batches[%s].end_date precedes start_date; skipped.', idx,
                )
                continue
            spec = {
                'budget_id': budget.id,
                'total_tasks': no_of_task,
                'start_date': start_date,
                'end_date': end_date,
                'buffer_pct': _coerce_float(
                    entry.get('buffer_pct'), budget_buffer,
                ),
            }
            name = (str(entry.get('name') or '')).strip()
            if name:
                spec['name'] = name
            specs.append(spec)

        if not specs:
            return [], None

        # Clone budget lines onto each phase. The phase model's create() hook
        # only auto-copies MODEL lines when none are supplied, so we pass all
        # three explicitly for deterministic, complete phases.
        phase_model_clone = [
            (0, 0, {
                'ai_model_id': ln.ai_model_id.id,
                'ai_model_name': ln.ai_model_name or False,
                'cost_type': ln.cost_type or 'per_task',
                'per_trajectory_cost': ln.per_trajectory_cost or 0.0,
                'iterations': ln.iterations or 0,
                'per_task_cost': ln.per_task_cost or 0.0,
            })
            for ln in budget.model_line_ids
        ]
        phase_infra_clone = [
            (0, 0, {
                'infra_type_id': ln.infra_type_id.id,
                'description': ln.description or False,
                'budget_amount': ln.budget_amount or 0.0,
                'instance_type': ln.instance_type or False,
                'unit_price_usd': ln.unit_price_usd or 0.0,
                'price_unit': ln.price_unit or False,
                'quantity': ln.quantity or 1.0,
                'duration_hours': ln.duration_hours or 730.0,
                'ebs_storage_gb': ln.ebs_storage_gb or 0.0,
                'volume_type': ln.volume_type or 'gp3',
                'volume_rate_usd_per_gb_mo': ln.volume_rate_usd_per_gb_mo or 0.0,
            })
            for ln in budget.infra_line_ids
        ]
        phase_sub_clone = [
            (0, 0, {
                'subscription_id': ln.subscription_id.id,
                'assigned_user_ids': [(6, 0, ln.assigned_user_ids.ids)],
            })
            for ln in budget.subscription_line_ids
        ]

        create_env = env(context=dict(
            env.context,
            tracking_disable=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            mail_notrack=True,
            ethara_skip_notify=True,
        ))
        Phase = create_env[PHASE_MODEL].sudo()

        created_phases = Phase.browse()
        for spec in specs:
            vals = dict(spec)
            if phase_model_clone:
                vals['model_line_ids'] = phase_model_clone
            if phase_infra_clone:
                vals['infra_line_ids'] = phase_infra_clone
            if phase_sub_clone:
                vals['subscription_line_ids'] = phase_sub_clone
            try:
                with env.cr.savepoint():
                    created_phases |= Phase.create(vals)
            except Exception:
                _logger.exception(
                    'Phase creation failed for budget %s spec %s',
                    budget.id, spec,
                )

        if not created_phases:
            return [], None

        phase_ids = created_phases.ids

        # Only auto-submit a request when there is something to fund.
        is_rnd = (budget.project_type == 'rnd')
        has_lines = bool(
            budget.model_line_ids
            or budget.infra_line_ids
            or budget.subscription_line_ids
        )
        if not (is_rnd or has_lines):
            return phase_ids, None

        first_phase = created_phases.sorted(
            key=lambda p: (p.start_date or fields.Date.today(), p.id)
        )[:1]
        if not first_phase:
            return phase_ids, None

        duration_days = 0
        if (
            first_phase.start_date
            and first_phase.end_date
            and first_phase.end_date >= first_phase.start_date
        ):
            # Inclusive of the end date (+1), matching the phase model's
            # `_compute_estimated_cost` (ethara_project_phase.py) and the
            # Flutter estimate (`_phaseDays`). Without the +1 the request's
            # infra requested_amount was one day short of the phase_budget,
            # e.g. a Jul 20 → Jul 30 window billed 10 days here vs 11 there.
            duration_days = (
                first_phase.end_date - first_phase.start_date
            ).days + 1

        req_total_tasks = first_phase.total_tasks or 0
        req_buffer_pct = first_phase.buffer_pct or 0.0

        # Build request line commands mirroring the budget's lines, with
        # requested_amount derived the same way the etp flow did.
        req_model_cmds = [
            (0, 0, {
                'ai_model_id': ln.ai_model_id.id,
                'ai_model_name': ln.ai_model_name or False,
                'cost_type': ln.cost_type or 'per_task',
                'per_task_cost': ln.per_task_cost or 0.0,
                'per_trajectory_cost': ln.per_trajectory_cost or 0.0,
                'iterations': ln.iterations or 0,
                'requested_amount': req_total_tasks * (ln.per_task_cost or 0.0),
            })
            for ln in budget.model_line_ids
        ]
        req_infra_cmds = [
            (0, 0, {
                'infra_type_id': ln.infra_type_id.id,
                'description': ln.description or False,
                'start_date': first_phase.start_date or False,
                'end_date': first_phase.end_date or False,
                'requested_amount': duration_days * ((ln.budget_amount or 0.0) / 30.0),
                'instance_type': ln.instance_type or False,
                'unit_price_usd': ln.unit_price_usd or 0.0,
                'price_unit': ln.price_unit or False,
                'quantity': ln.quantity or 1.0,
                'duration_hours': ln.duration_hours or 730.0,
                'ebs_storage_gb': ln.ebs_storage_gb or 0.0,
                'volume_type': ln.volume_type or 'gp3',
                'volume_rate_usd_per_gb_mo': ln.volume_rate_usd_per_gb_mo or 0.0,
            })
            for ln in budget.infra_line_ids
        ]
        req_sub_cmds = [
            (0, 0, {
                'subscription_id': ln.subscription_id.id,
                'assigned_user_ids': [(6, 0, ln.assigned_user_ids.ids)],
                'requested_amount': (
                    (ln.cost_per_subscription or 0.0)
                    * len(ln.assigned_user_ids)
                ),
            })
            for ln in budget.subscription_line_ids
        ]

        buffer_factor = 1.0 + (req_buffer_pct / 100.0)
        model_total = sum(c[2]['requested_amount'] for c in req_model_cmds)
        infra_total = sum(c[2]['requested_amount'] for c in req_infra_cmds)
        sub_total = sum(c[2]['requested_amount'] for c in req_sub_cmds)
        if is_rnd and (budget.budget_amount or 0.0) > 0.0:
            requested_total = budget.budget_amount
        else:
            requested_total = (
                (model_total + infra_total) * buffer_factor + sub_total
            )

        justification = (jdata.get('justification') or '').strip() or (
            "Auto-generated from project budget '%s' creation."
            % (budget.name or '')
        )

        req_vals = {
            'phase_id': first_phase.id,
            'requester_id': env.user.id,
            'request_type': 'budget',
            'justification': justification,
            'priority': budget.priority or 'normal',
            'total_tasks': req_total_tasks,
            'buffer_pct': req_buffer_pct,
            'requested_total': requested_total,
        }
        if req_model_cmds:
            req_vals['model_line_ids'] = req_model_cmds
        if req_infra_cmds:
            req_vals['infra_line_ids'] = req_infra_cmds
        if req_sub_cmds:
            req_vals['subscription_line_ids'] = req_sub_cmds

        request_id = None
        try:
            with env.cr.savepoint():
                Request = create_env[REQUEST_MODEL].sudo()
                req = Request.create(req_vals)
                request_id = req.id
                try:
                    with env.cr.savepoint():
                        req.with_context(
                            ethara_skip_notify=True,
                        ).action_submit_for_approval()
                except (UserError, ValidationError) as e:
                    _logger.info(
                        'Auto-submit left request %s in draft for budget %s: %s',
                        req.id, budget.id, e,
                    )
                except Exception:
                    _logger.exception(
                        'Auto-submit failed for request %s (budget %s); '
                        'left in draft.', req.id, budget.id,
                    )
        except Exception:
            _logger.exception(
                'Auto phase request creation failed for budget %s.', budget.id,
            )
            request_id = None

        return phase_ids, request_id

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

            # R&D budgets carry a sub-type (testing|sampling); it scopes the
            # duplicate check below so a project can hold one budget per
            # sub-type. Required and validated for R&D; ignored otherwise.
            sub_type = (jdata.get('budget_sub_type') or '').strip().lower()
            if is_rnd:
                if sub_type not in VALID_RND_SUB_TYPES:
                    return return_Response(
                        message=(
                            'budget_sub_type must be one of '
                            f'{list(VALID_RND_SUB_TYPES)} for R&D budgets.'
                        ),
                        status=400, data={},
                    )
            else:
                sub_type = ''

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

            # Uniqueness is per (project, type) for operations, but per
            # (project, type, sub-type) for R&D — so rnd/testing and
            # rnd/sampling can coexist while a second rnd/testing is blocked.
            dup_domain = [
                ('ethara_project_id', '=', project_id),
                ('project_type', '=', budget_type),
            ]
            if is_rnd:
                dup_domain.append(('budget_sub_type', '=', sub_type))
            dup = request.env[BUDGET_MODEL].sudo().search(dup_domain, limit=1)
            if dup:
                label = f'{budget_type} ({sub_type})' if is_rnd else budget_type
                return return_Response(
                    message=(
                        f'A {label} budget already exists for project '
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

            # R&D sub-type (testing | sampling) — validated above; only stored
            # for R&D budgets (empty string for operations).
            if is_rnd and sub_type:
                vals['budget_sub_type'] = sub_type

            budget = request.env[BUDGET_MODEL].sudo().with_context(
                tracking_disable=True,
                mail_create_nolog=True,
                mail_create_nosubscribe=True,
                mail_notrack=True,
                ethara_skip_notify=True,
            ).create(vals)

            # -----------------------------------------------------------------
            # Notify approver users via mail template.
            # Uses the single-thread email pattern - notifications land on the
            # parent ethara.project chatter so all 13 budget-lifecycle emails
            # stay in one conversation.
            # force_send=False -> queued in mail.mail, drained by mail cron.
            # -----------------------------------------------------------------
            try:
                approver_partner_ids = list({
                    u.partner_id.id
                    for u in request.env['res.users'].sudo().browse(approver_ids)
                    if u.partner_id
                })
                if approver_partner_ids and hasattr(project, '_ethara_post_thread_message'):
                    dbname = request.env.cr.dbname
                    uid = request.env.uid
                    ctx = dict(request.env.context)
                    project_id = project.id
                    budget_id = budget.id
                    partner_ids_snapshot = list(approver_partner_ids)

                    def _launch():
                        def _run():
                            try:
                                registry = Registry(dbname)
                                with registry.cursor() as new_cr:
                                    new_env = api.Environment(new_cr, uid, ctx)
                                    proj = new_env['ethara.project'].browse(project_id).exists()
                                    bud = new_env['ethara.project.budget'].browse(budget_id).exists()
                                    if proj and bud:
                                        proj.sudo()._ethara_post_thread_message(
                                            'ethara_project.mail_template_ethara_project_budget_created',
                                            bud,
                                            partner_ids_snapshot,
                                        )
                                    new_cr.commit()
                            except Exception:
                                _logger.exception(
                                    'Deferred budget-created mail failed for budget %s',
                                    budget_id,
                                )
                        threading.Thread(target=_run, daemon=True).start()

                    request.env.cr.postcommit.add(_launch)
            except Exception:
                _logger.exception(
                    'ethara_project budget/create: mail notification failed '
                    '(budget id=%s) - budget still created successfully',
                    budget.id,
                )

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

            phase_ids, request_id = (
                EtharaBudgetController
                ._create_phases_and_submit_request(
                    request.env, budget.id, jdata,
                )
            )

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
                    'phase_ids': phase_ids,
                    'request_id': request_id,
                }},
            )
        except Exception as e:
            _logger.exception('ethara_project budget/create failed')
            return return_Response(
                message='Failed to create Ethara project budget.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/project_members?project_id=<int>
    #
    # Users assigned to a specific ethara.project — feeds the create-budget
    # Step-2 subscription seat picker. Unions the project's three team M2Ms
    # (assigned_tpm_ids / assigned_pl_ql_ids / assigned_rnd_ids, all
    # hr.employee) mapped through user_id to res.users. Returns the historical
    # nested `data.options[]` shape the Flutter ApproverOptionsResponseModel
    # was built to parse.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/project_members',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def list_project_members(self, **params):
        try:
            pid = _coerce_int(params.get('project_id'))
            if not pid:
                return return_Response(
                    message="'project_id' query parameter is required.",
                    status=400, data={},
                )
            Project = request.env[PROJECT_MODEL].sudo()
            project = Project.browse(pid).exists()
            if not project:
                return return_Response(
                    message='Project %s not found.' % pid,
                    status=400, data={},
                )
            # Union every team M2M that exists on the schema (stays safe if a
            # deployment lacks one of them).
            team_fields = (
                'assigned_tpm_ids', 'assigned_pl_ql_ids', 'assigned_rnd_ids',
            )
            employees = request.env['hr.employee'].sudo()
            for fname in team_fields:
                if fname in Project._fields:
                    employees |= project[fname]
            users = employees.mapped('user_id').filtered(
                lambda u: u.active and not u.share
            ).sorted(key=lambda u: (u.name or '').lower())
            options = [_serialize_user(u) for u in users]
            return return_Response(
                message='Project members fetched.', status=200,
                data={'data': {'total': len(options), 'options': options}},
            )
        except Exception as e:
            _logger.exception('ethara_project budget/project_members failed')
            return return_Response(
                message='Failed to list project members.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra/services
    #
    # AWS-managed infrastructure catalog (rows in ethara.project.infra.type
    # with is_aws_managed=True). Feeds the AWS service picker on the
    # Infrastructure Lines tab. Query: search, limit, offset.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/services',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def aws_pricing_services(self, **params):
        try:
            limit, offset, err = _pagination(params)
            if err:
                return err
            domain = [('is_aws_managed', '=', True), ('active', '=', True)]
            search = (params.get('search') or '').strip()
            if search:
                domain += [
                    '|',
                    ('name', 'ilike', search),
                    ('aws_service_code', 'ilike', search),
                ]
            Infra = request.env[INFRA_MODEL].sudo()
            total = Infra.search_count(domain)
            records = Infra.search(
                domain, limit=limit, offset=offset, order='sequence, name',
            )
            items = [
                {
                    'id': r.id,
                    'service_code': r.aws_service_code or '',
                    'name': r.name or '',
                    'primary_attr': r.primary_attr or '',
                    'item_count': r.item_count or 0,
                    'last_synced_at': (
                        r.last_synced_at.isoformat()
                        if r.last_synced_at else None
                    ),
                }
                for r in records
            ]
            return return_Response(
                message='AWS pricing services fetched.', status=200,
                data={
                    'total': total, 'limit': limit, 'offset': offset,
                    'services': items,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/services failed')
            return return_Response(
                message='Failed to list AWS pricing services.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra/items
    #
    # Instance/item catalog for one AWS service (rows in
    # ethara.project.aws.pricing.item). Auto-fetches from AWS on first call
    # via aws_pricing_service.fetch_items_for_service (cached 24h).
    # Query: type_id | service (aws_service_code), search, force_refresh.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/items',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def aws_pricing_items(self, **params):
        try:
            Infra = request.env[INFRA_MODEL].sudo()
            type_id = _coerce_int(params.get('type_id'))
            service = (params.get('service') or '').strip()
            infra = Infra
            if type_id:
                infra = Infra.browse(type_id).exists()
            elif service:
                infra = Infra.search(
                    [('aws_service_code', '=ilike', service)], limit=1,
                )
            else:
                return return_Response(
                    message="'type_id' or 'service' is required.",
                    status=400, data={},
                )
            if not infra:
                return return_Response(
                    message='Infrastructure type not found.',
                    status=400, data={},
                )
            force_refresh = bool(_coerce_int(params.get('force_refresh'), 0))
            from_cache = True
            if infra.aws_service_code and infra.primary_attr:
                stats = aws_pricing_service.fetch_items_for_service(
                    request.env, infra, force_refresh=force_refresh,
                )
                from_cache = bool(stats.get('from_cache', True))
            limit, offset, err = _pagination(params)
            if err:
                return err
            Item = request.env['ethara.project.aws.pricing.item'].sudo()
            idomain = [('infra_type_id', '=', infra.id), ('active', '=', True)]
            search = (params.get('search') or '').strip()
            if search:
                idomain.append(('name', 'ilike', search))
            total = Item.search_count(idomain)
            records = Item.search(
                idomain, limit=limit, offset=offset, order='name',
            )
            items = [
                {
                    'id': r.id,
                    'name': r.name or '',
                    'sku_count': r.sku_count or 0,
                }
                for r in records
            ]
            return return_Response(
                message='AWS pricing items fetched.', status=200,
                data={
                    'infra_type_id': infra.id,
                    'service_code': infra.aws_service_code or '',
                    'primary_attr': infra.primary_attr or '',
                    'total': total, 'limit': limit, 'offset': offset,
                    'items': items,
                    'from_cache': from_cache,
                },
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/items failed')
            return return_Response(
                message='Failed to list AWS pricing items.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra/pricing
    #
    # On-demand pricing for one service+item+region via
    # aws_pricing_service.fetch_pricing_cached. Response fields are returned
    # FLAT at the envelope root (service_code, item, region, min_price, unit,
    # specs, varying, rows) — Flutter AwsPricingResponseModel reads root-level.
    # Query: service*, item*, region (label; default "US East (N. Virginia)"),
    # force_refresh.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/pricing',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def aws_pricing_detail(self, **params):
        try:
            service = (params.get('service') or '').strip()
            item = (params.get('item') or '').strip()
            if not service or not item:
                return return_Response(
                    message="'service' and 'item' are required.",
                    status=400, data={},
                )
            region = (params.get('region') or '').strip() \
                or 'US East (N. Virginia)'
            force_refresh = bool(_coerce_int(params.get('force_refresh'), 0))
            pricing = aws_pricing_service.fetch_pricing_cached(
                request.env, service, item, region,
                force_refresh=force_refresh,
            )
            if not pricing:
                return return_Response(
                    message='Pricing not found for %s / %s.' % (service, item),
                    status=400, data={},
                )
            return return_Response(
                message='Pricing fetched.', status=200, data=pricing,
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/pricing failed')
            return return_Response(
                message='Failed to fetch AWS pricing.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra/ebs_pricing
    #
    # EBS/volume list prices for a region via
    # aws_pricing_service.fetch_ebs_pricing_cached. Returns top-level
    # `volumes[]` (Flutter EbsVolumesResponseModel uses the tolerant parser).
    # Query: region (label; default "US East (N. Virginia)"), force_refresh.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/ebs_pricing',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def aws_ebs_pricing(self, **params):
        try:
            region = (params.get('region') or '').strip() \
                or 'US East (N. Virginia)'
            force_refresh = bool(_coerce_int(params.get('force_refresh'), 0))
            result = aws_pricing_service.fetch_ebs_pricing_cached(
                request.env, region, force_refresh=force_refresh,
            )
            return return_Response(
                message='EBS pricing fetched.', status=200, data=result,
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/ebs_pricing failed')
            return return_Response(
                message='Failed to fetch EBS pricing.',
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

    # -----------------------------------------------------------------------
    # GET /api/v2/project_team_member_list?project_id=<id>
    #
    # URL-compat alias for the legacy task_forge_bridge Team-tab endpoint.
    # Flutter's Project Details page has this URL hardcoded and fires it
    # whenever the Team tab loads. Since task_forge_bridge is uninstalled,
    # ethara_project owns this URL and answers from its own ethara.project
    # M2M assignments (assigned_tpm_ids + assigned_pl_ql_ids + assigned_rnd_ids)
    # so nothing external is required.
    #
    # Query params:
    #   - project_id (int, required)   ethara.project id
    #   - active     (str, optional)   'false' to fetch inactive employees
    #   - search     (str, optional)   name substring filter
    #   - role       (int, optional)   api.role id filter
    #
    # Response envelope (matches legacy task_forge shape exactly):
    #   {message: "N employees found", errors: [], status_code: 200,
    #    data: {
    #      total: N,
    #      data: [{
    #        id, name, email, job_title_id, job_title, department_id,
    #        department, offboarding_state, role_id, role, total_done_task,
    #        pl_id, pl_name, qr_id, qr_name, active, avg_time, since
    #      }]
    #    }}
    #
    # Fields ethara does not own return safe defaults:
    #   offboarding_state = ''; total_done_task = 0; avg_time = 0
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v2/project_team_member_list',
        methods=['GET'], type='http', auth='none', csrf=False, cors='*',
    )
    @validate_token
    def project_team_member_list(self, **params):
        try:
            project_id = _coerce_int(params.get('project_id'))
            if not project_id:
                return return_Response(
                    message='project_id is required.', status=400, data={},
                )
            project = request.env[PROJECT_MODEL].sudo().browse(project_id).exists()
            if not project:
                return return_Response(
                    message='Project not found', status=404, data={},
                )
            team = (
                project.assigned_tpm_ids
                | project.assigned_pl_ql_ids
                | project.assigned_rnd_ids
            )
            pl_first = project.assigned_pl_ql_ids[:1]
            qr_first = project.assigned_pl_ql_ids[1:2] or project.assigned_pl_ql_ids[:1]
            active_filter = (params.get('active') or '').strip().lower()
            search_filter = (params.get('search') or '').strip()
            role_filter = _coerce_int(params.get('role'))
            filtered = team
            if active_filter == 'false':
                filtered = filtered.filtered(lambda e: not e.active)
            else:
                filtered = filtered.filtered(lambda e: e.active)
            if search_filter:
                s = search_filter.lower()
                filtered = filtered.filtered(lambda e: s in (e.name or '').lower())
            if role_filter:
                filtered = filtered.filtered(
                    lambda e: e.user_id and e.user_id.user_role
                    and e.user_id.user_role.id == role_filter
                )
            rows = []
            for emp in filtered:
                user = emp.user_id
                role_rec = user.user_role if user and user.user_role else False
                rows.append({
                    'id': emp.id,
                    'name': emp.name or '',
                    'email': emp.work_email or '',
                    'job_title_id': emp.job_id.id if emp.job_id else 0,
                    'job_title': emp.job_id.name if emp.job_id else '',
                    'department_id': emp.department_id.id if emp.department_id else 0,
                    'department': emp.department_id.name if emp.department_id else '',
                    'offboarding_state': '',
                    'role_id': role_rec.id if role_rec else 0,
                    'role': role_rec.name if role_rec else '',
                    'total_done_task': 0,
                    'pl_id': pl_first.id if pl_first else 0,
                    'pl_name': pl_first.name if pl_first else '',
                    'qr_id': qr_first.id if qr_first else 0,
                    'qr_name': qr_first.name if qr_first else '',
                    'active': bool(emp.active),
                    'avg_time': 0,
                    'since': str(emp.create_date.date()) if emp.create_date else '',
                })
            return return_Response(
                message='%s employees found' % len(rows),
                status=200,
                data={'total': len(rows), 'data': rows},
            )
        except Exception as e:
            _logger.exception('project_team_member_list failed')
            return return_Response(
                message=str(e), status=400, errors=[str(e)],
            )


