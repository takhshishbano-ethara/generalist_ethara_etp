# -*- coding: utf-8 -*-
"""ethara_project budget request/approval HTTP API (Group C).

Thin HTTP controllers over the ALREADY-BUILT `ethara.project.phase.request`
state machine. Ports the seven `/budget/requests/*` endpoints from
`etp_projects` onto the canonical `/api/v1/ethara_project/...` namespace.

Field-name deltas applied throughout (etp -> ethara):

    etp.batch.budget.request   -> ethara.project.phase.request
    request.batch_id           -> request.phase_id      (FK to ethara.project.phase)
    request.project_budget_id  -> request.budget_id
    request.project_id         -> request.ethara_project_id
    etp.batch.budget           -> ethara.project.phase
    etp.ai.model               -> ethara.project.ai.model
    etp.infra.type             -> ethara.project.infra.type
    etp.subscription           -> ethara.project.subscription

The ethara request line models drop a handful of etp-only columns
(`usage_tag`/`description` on model lines, `per_day_requested`/
`per_day_approved` on infra lines, `per_day_cost` on subscription lines); those
JSON keys are emitted as safe defaults so the existing Flutter approval models
keep parsing. The phase (batch) uses `phase_budget` where etp used
`batch_budget`.

The Flutter frontend still sends the body/param key `batch_id`; it is accepted
verbatim and resolved as the `ethara.project.phase` id.

Endpoints:
- GET  /api/v1/ethara_project/budget/requests/list
- GET  /api/v1/ethara_project/budget/requests/detail
- POST /api/v1/ethara_project/budget/requests/create
- POST /api/v1/ethara_project/budget/requests/update
- POST /api/v1/ethara_project/budget/requests/approve      (body step: cto|cfo)
- POST /api/v1/ethara_project/budget/requests/reject       (body step, note)
- POST /api/v1/ethara_project/budget/requests/mark_review  (body step, note?)
"""
import base64
import logging

from odoo import http, fields
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .budget_api import (
    _coerce_int,
    _coerce_float,
    _coerce_bool,
    _coerce_date,
    _pagination,
    _read_multipart_or_json,
    _missing_ids,
)
from ..models.role_map import resolve_role_ids

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUEST_MODEL = 'ethara.project.phase.request'
PHASE_MODEL = 'ethara.project.phase'
BUDGET_MODEL = 'ethara.project.budget'
AI_MODEL = 'ethara.project.ai.model'
INFRA_MODEL = 'ethara.project.infra.type'
SUBSCRIPTION_MODEL = 'ethara.project.subscription'
TOPUP_REASON_MODEL = 'ethara.project.phase.topup.reason'

VALID_PRIORITIES = ('low', 'normal', 'high', 'urgent')
VALID_COST_TYPES = ('per_task', 'per_trajectory')


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def _parse_attachment_links(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(',') if s.strip()]
    return []


def _serialize_attachment_links(urls):
    seen = []
    for u in urls or []:
        if u and u not in seen:
            seen.append(u)
    return ','.join(seen)


def _sync_attachment_urls_from_ids(record):
    if not record or 'attachment_urls' not in record._fields:
        return
    urls = []
    for att in record.attachment_ids:
        if att.type == 'url' and att.url:
            urls.append(att.url)
    record.sudo().with_context(
        tracking_disable=True, mail_notrack=True,
    ).write({'attachment_urls': _serialize_attachment_links(urls) or False})


def _upload_files_as_url_attachments(files, res_model, res_id):
    """Persist uploaded files as binary ir.attachment rows.

    Returns (list_of_attachment_ids, list_of_web_content_urls).
    """
    new_ids = []
    new_urls = []
    for f in files or []:
        try:
            data = f.read()
        except Exception:
            continue
        if not data:
            continue
        att = request.env['ir.attachment'].sudo().create({
            'name': getattr(f, 'filename', '') or 'attachment.bin',
            'type': 'binary',
            'datas': base64.b64encode(data).decode('utf-8'),
            'res_model': res_model,
            'res_id': res_id,
            'public': False,
        })
        new_ids.append(att.id)
        new_urls.append(f'/web/content/{att.id}?download=1')
    return new_ids, new_urls


# ---------------------------------------------------------------------------
# Label / brief serializers
# ---------------------------------------------------------------------------

def _request_state_label(value):
    selection = request.env[REQUEST_MODEL]._fields['state'].selection
    return dict(selection).get(value, value)


def _request_type_label(value):
    if 'request_type' not in request.env[REQUEST_MODEL]._fields:
        return value
    selection = request.env[REQUEST_MODEL]._fields['request_type'].selection
    return dict(selection).get(value, value)


def _budget_project_type(req):
    """The budget's real type (`rnd`/`operations`) so the client does not have
    to guess R&D vs Operations from the budget name."""
    budget = req.budget_id
    if not budget or 'project_type' not in budget._fields:
        return ''
    return budget.project_type or ''


def _budget_sub_type_value(req):
    """Raw R&D sub-type (`testing`/`sampling`) off the parent budget, guarded so
    the controller survives a code reload that precedes the `-u` schema add."""
    budget = req.budget_id
    if not budget or 'budget_sub_type' not in budget._fields:
        return ''
    return budget.budget_sub_type or ''


def _budget_sub_type_label(req):
    value = _budget_sub_type_value(req)
    if not value:
        return ''
    budget = req.budget_id
    selection = dict(budget._fields['budget_sub_type'].selection)
    return selection.get(value, value.title())


def _budget_team_type_value(req):
    """Owning team (`generalist`/`technical`/`rnd`) off the parent budget,
    guarded so the controller survives a code reload preceding the `-u`."""
    budget = req.budget_id
    if not budget or 'team_type' not in budget._fields:
        return ''
    return budget.team_type or ''


def _budget_team_type_label(req):
    value = _budget_team_type_value(req)
    if not value:
        return ''
    budget = req.budget_id
    selection = dict(budget._fields['team_type'].selection)
    return selection.get(value, value.title())


def _role_brief(role):
    if not role:
        return None
    return {
        'id': role.id,
        'name': role.name or '',
        'user_type': getattr(role, 'user_type', '') or '',
    }


def _user_brief(user):
    if not user:
        return None
    return {
        'id': user.id,
        'name': user.name or '',
        'email': user.email or (
            user.partner_id.email if user.partner_id else ''
        ) or '',
        'role': (
            _role_brief(user.user_role) if 'user_role' in user._fields else None
        ),
    }


def _user_role_code(user):
    """The canonical role string ('CTO'/'CFO'/…) for a user, taken from their
    `user_role.user_type`. Empty when the user or role is missing. Matches the
    `user_type` the Flutter side feeds to `UserRole.fromApiString`, so the
    approval trail can tell WHICH role acted (e.g. a CTO send-back vs a CTO
    approve-and-forward)."""
    if not user or 'user_role' not in user._fields:
        return ''
    role = user.user_role
    if not role:
        return ''
    return getattr(role, 'user_type', '') or ''


def _author_brief(partner):
    if not partner:
        return None
    user = partner.user_ids[:1]
    role = user.user_role if user and 'user_role' in user._fields else False
    return {
        'id': partner.id,
        'name': partner.name or '',
        'email': partner.email or '',
        'role': _role_brief(role) if role else None,
    }


def _with_date(user_dict, date_value):
    if user_dict is None:
        return None
    return {
        **user_dict,
        'date': (
            fields.Datetime.to_string(date_value) if date_value else None
        ),
    }


def _topup_reason_brief(r):
    if not r:
        return None
    return {
        'id': r.id,
        'name': r.name or '',
        'code': getattr(r, 'code', '') or '',
        'description': getattr(r, 'description', '') or '',
        'sequence': getattr(r, 'sequence', 0) or 0,
        'active': bool(r.active),
    }


def _attachment_brief(att):
    if not att:
        return None
    return {
        'id': att.id,
        'name': att.name or '',
        'mimetype': att.mimetype or '',
        'file_size': att.file_size or 0,
        'checksum': att.checksum or '',
        'url': f'/web/content/{att.id}?download=1',
    }


def _tracking_value_brief(tv):
    try:
        def _pick(prefix):
            for suffix in (
                'char', 'text', 'integer', 'float', 'monetary', 'datetime',
            ):
                fname = f'{prefix}_{suffix}'
                if fname in tv._fields:
                    val = tv[fname]
                    if val:
                        # Datetime tracking values are raw datetime objects,
                        # which json.dumps can't serialize — stringify them.
                        if suffix == 'datetime':
                            return fields.Datetime.to_string(val)
                        return val
            return None
        field_name = ''
        field_label = ''
        if tv.field_id:
            field_name = tv.field_id.name or ''
            field_label = tv.field_id.field_description or ''
        elif 'field_info' in tv._fields and tv.field_info:
            field_name = tv.field_info.get('name', '') or ''
            field_label = tv.field_info.get('desc', '') or ''
        return {
            'field': field_name,
            'field_label': field_label,
            'old_value': _pick('old_value'),
            'new_value': _pick('new_value'),
        }
    except Exception:
        _logger.exception(
            'Failed to serialize tracking value id=%s',
            getattr(tv, 'id', None),
        )
        return None


def _change_log(record, limit=200):
    try:
        msgs = record.message_ids.sorted(
            key=lambda m: m.date or m.create_date, reverse=True,
        )[:limit]
    except Exception:
        _logger.exception(
            'Failed to read message_ids for %s(%s)', record._name, record.id,
        )
        return []
    log = []
    for m in msgs:
        try:
            log.append({
                'id': m.id,
                'date': fields.Datetime.to_string(m.date) if m.date else None,
                'author': _author_brief(m.author_id),
                'subject': str(m.subject) if m.subject else '',
                'body': str(m.body) if m.body else '',
                'message_type': m.message_type or '',
                'subtype': (
                    str(m.subtype_id.name)
                    if m.subtype_id and m.subtype_id.name else ''
                ),
                'tracking_values': [
                    tvb for tvb in (
                        _tracking_value_brief(tv)
                        for tv in m.tracking_value_ids
                    ) if tvb is not None
                ],
            })
        except Exception:
            _logger.exception(
                'Failed to serialize message id=%s', getattr(m, 'id', None),
            )
    return log


def _share_pct(amount, total):
    if not total:
        return 0.0
    return round((amount or 0.0) / total * 100.0, 2)


# ---------------------------------------------------------------------------
# Phase (batch) serializers
# ---------------------------------------------------------------------------

def _batch_short(batch):
    if not batch:
        return None
    return {
        'id': batch.id,
        'name': batch.name,
        'state': batch.state,
        'total_tasks': batch.total_tasks,
        'done_tasks': batch.done_tasks,
        'remaining_tasks': batch.remaining_tasks,
        'start_date': batch.start_date.isoformat() if batch.start_date else None,
        'end_date': batch.end_date.isoformat() if batch.end_date else None,
    }


def _batch_detail(batch):
    if not batch:
        return None
    model_lines = [
        {
            'id': ln.id,
            'ai_model_id': ln.ai_model_id.id,
            'ai_model_name': ln.ai_model_name or '',
            'model_name': (
                ln.ai_model_name
                or ln.ai_model_id.display_name
                or ln.ai_model_id.name
            ),
            'usage_tag': (
                ln.usage_tag if 'usage_tag' in ln._fields
                else 'trajectory_building'
            ),
            'cost_type': ln.cost_type,
            'per_task_cost': ln.per_task_cost,
            'per_trajectory_cost': ln.per_trajectory_cost,
            'iterations': ln.iterations,
        }
        for ln in batch.model_line_ids
    ]
    infra_lines = [
        {
            'id': ln.id,
            'infra_id': ln.infra_type_id.id,
            'infra_name': ln.infra_type_id.display_name or ln.infra_type_id.name,
            'description': ln.description or '',
            'cost': ln.budget_amount,
            'per_day_cost': ln.per_day_cost,
            'start_date': ln.start_date.isoformat() if ln.start_date else None,
            'end_date': ln.end_date.isoformat() if ln.end_date else None,
            'instance_type': ln.instance_type or '',
            'unit_price_usd': ln.unit_price_usd or 0.0,
            'price_unit': ln.price_unit or '',
            'quantity': ln.quantity or 0.0,
            'duration_hours': ln.duration_hours or 0.0,
            'ebs_storage_gb': ln.ebs_storage_gb or 0.0,
            'volume_type': ln.volume_type or '',
            'volume_rate_usd_per_gb_mo': ln.volume_rate_usd_per_gb_mo or 0.0,
            'compute_amount': (
                ln.compute_amount if 'compute_amount' in ln._fields else 0.0
            ),
            'storage_amount': (
                ln.storage_amount if 'storage_amount' in ln._fields else 0.0
            ),
            'computed_amount': (
                ln.computed_amount if 'computed_amount' in ln._fields else 0.0
            ),
        }
        for ln in batch.infra_line_ids
    ]
    sub_lines = [
        {
            'id': ln.id,
            'subscription_id': ln.subscription_id.id,
            'subscription_name': (
                ln.subscription_id.display_name or ln.subscription_id.name
            ),
            'cost_per_seat': ln.cost_per_subscription,
            'assigned_to': ln.assigned_user_ids.ids,
            'seat_count': ln.subscription_count,
            'monthly_total': ln.final_amount,
            'per_day_cost': ln.per_day_cost,
            'approved_amount': ln.approved_amount,
        }
        for ln in batch.subscription_line_ids
    ]
    short = _batch_short(batch)
    short.update({
        'buffer_pct': batch.buffer_pct,
        'estimated_cost': batch.estimated_cost,
        'batch_budget': batch.phase_budget,
        'approved_amount': batch.approved_amount,
        'consumed_cost': batch.consumed_cost,
        'consumed_pct': batch.consumed_pct,
        'remaining_cost': batch.remaining_cost,
        'attachment_urls': [
            _attachment_brief(a)['url']
            for a in request.env['ir.attachment'].sudo().search([
                ('res_model', '=', PHASE_MODEL),
                ('res_id', '=', batch.id),
            ], order='id')
        ],
        'model_lines': model_lines,
        'infra_lines': infra_lines,
        'subscription_lines': sub_lines,
    })
    return short


def _budget_overview(budget):
    if not budget:
        return None
    phases = request.env[PHASE_MODEL].sudo().search(
        [('budget_id', '=', budget.id)], order='start_date, id',
    )
    done_overall = sum(phases.mapped('done_tasks'))
    remaining_overall = sum(phases.mapped('remaining_tasks'))
    return {
        'id': budget.id,
        'name': budget.name or '',
        'total_tasks': budget.total_tasks,
        'done_tasks_overall': done_overall,
        'remaining_tasks_overall': remaining_overall,
        'batches': [_batch_short(b) for b in phases],
    }


# ---------------------------------------------------------------------------
# Request serializers
# ---------------------------------------------------------------------------

def _request_to_summary(req):
    req.ensure_one()
    has_request_type = 'request_type' in req._fields
    cto_reviewer = getattr(req, 'cto_reviewer_id', False)
    cto_review_date = getattr(req, 'cto_review_date', False)
    cfo_approver = getattr(req, 'cfo_approver_id', False)
    cfo_approval_date = getattr(req, 'cfo_approval_date', False)
    return {
        'id': req.id,
        'name': req.name,
        'state': req.state,
        'state_label': _request_state_label(req.state),
        'request_type': req.request_type if has_request_type else None,
        'request_type_label': (
            _request_type_label(req.request_type) if has_request_type else None
        ),
        'revision_no': getattr(req, 'revision_no', 0) or 0,
        'request_date': (
            fields.Datetime.to_string(req.request_date)
            if req.request_date else None
        ),
        'priority': req.priority,
        'requester': _with_date(
            _user_brief(req.requester_id), req.request_date,
        ),
        'cto_reviewer': _with_date(_user_brief(cto_reviewer), cto_review_date),
        'cto_review_date': (
            fields.Datetime.to_string(cto_review_date)
            if cto_review_date else None
        ),
        'cfo_approver': _with_date(_user_brief(cfo_approver), cfo_approval_date),
        'cfo_approval_date': (
            fields.Datetime.to_string(cfo_approval_date)
            if cfo_approval_date else None
        ),
        'approver': _with_date(_user_brief(req.approver_id), req.approval_date),
        'approval_date': (
            fields.Datetime.to_string(req.approval_date)
            if req.approval_date else None
        ),
        # WHO sent it back and in WHICH role — needed by the approval trail
        # (on list cards too) to tell a CTO send-back from a CTO
        # approve-and-forward. Without the role the trail always read
        # "forwarded to CFO" even for a returned request.
        'rejected_by': req.rejected_by.name if req.rejected_by else '',
        'rejected_by_role': _user_role_code(req.rejected_by),
        'batch': {
            'id': req.phase_id.id,
            'name': req.phase_id.name,
            'state': req.phase_id.state,
        } if req.phase_id else None,
        'project': {
            'id': req.ethara_project_id.id,
            'name': req.ethara_project_id.display_name,
        } if req.ethara_project_id else None,
        'project_budget': {
            'id': req.budget_id.id,
            'name': req.budget_id.name or '',
        } if req.budget_id else None,
        'project_budget_id': req.budget_id.id if req.budget_id else None,
        'project_type': _budget_project_type(req),
        'team_type': _budget_team_type_value(req),
        'team_type_label': _budget_team_type_label(req),
        'budget_sub_type': _budget_sub_type_value(req),
        'budget_sub_type_label': _budget_sub_type_label(req),
        'total_tasks': req.total_tasks,
        'requested_total': req.requested_total,
        'approved_total': (
            req.approved_total
            if req.state in ('approved', 'partially_approved') else 0.0
        ),
        'remaining_amount': req.remaining_amount,
        'sequence_number': req.sequence_number,
    }


def _caller_is_cfo():
    """True when the authenticated caller holds a CFO role. `@validate_token`
    sets `request.env.user` to the token's user (via update_env), so this
    reflects the HTTP caller even when the record itself is browsed sudo."""
    user = request.env.user
    role = getattr(user, 'user_role', False)
    if not role:
        return False
    return role.id in resolve_role_ids(request.env, 'cfo')


# Buffer figures are a CFO-confidential reserve ("Not visible to TPM/CTO").
# They are stripped from the detail payload for every non-CFO caller.
_BUFFER_TOPLEVEL_KEYS = ('buffer_pct', 'buffer_amount')
_BUFFER_TOTALS_KEYS = (
    'buffer_pct', 'buffer_amount',
    'buffer_amount_requested', 'buffer_amount_approved',
)
# The activity-log subject used by the set_buffer message_post. Shared between
# the post site and the redaction filter so they can't drift — a buffer message
# reveals the exact reserve, so it must be scrubbed from the change_log for
# non-CFO callers just like the buffer fields.
BUFFER_LOG_SUBJECT = 'Buffer reserve updated'

# Subject stamped on the chatter entry logged when a raiser revises + resubmits
# a returned budget, so the detail activity log shows WHAT changed and WHO did it.
RESUBMIT_LOG_SUBJECT = 'Budget revised & resubmitted'


def _esc(value):
    """Minimal HTML escape for user-controlled names in a message_post body."""
    return (
        str(value or '')
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def _request_line_snapshot(req):
    """A name-keyed snapshot of a request's cost lines, used to diff what the
    raiser changed on resubmit (lines are replaced wholesale, so we match on
    the human name rather than a stable id)."""
    snap = {'Models': {}, 'Infrastructure': {}, 'Subscriptions': {}}
    for line in req.model_line_ids:
        name = line.ai_model_name or (
            line.ai_model_id.name if line.ai_model_id else 'Model'
        )
        snap['Models'][name] = {'amount': line.requested_amount or 0.0}
    for line in req.infra_line_ids:
        name = line.instance_type or (
            line.infra_type_id.display_name or line.infra_type_id.name
            if line.infra_type_id else 'Infrastructure'
        )
        snap['Infrastructure'][name] = {
            'amount': line.requested_amount or 0.0,
            'qty': line.quantity or 0.0,
        }
    for line in req.subscription_line_ids:
        name = line.subscription_id.name if line.subscription_id else 'Subscription'
        snap['Subscriptions'][name] = {
            'amount': line.requested_amount or 0.0,
            'seats': getattr(line, 'subscription_count', 0) or 0,
        }
    return snap


def _diff_request_lines(before, after):
    """Build human-readable change lines from two `_request_line_snapshot`s:
    added / removed lines, amount changes, and infra quantity changes."""
    out = []
    for group in ('Models', 'Infrastructure', 'Subscriptions'):
        b = before.get(group, {})
        a = after.get(group, {})
        for name, av in a.items():
            bv = b.get(name)
            if bv is None:
                out.append(
                    f'Added {group[:-1] if group.endswith("s") else group} '
                    f'"{_esc(name)}" (USD {av["amount"]:,.2f})'
                )
            elif abs(av['amount'] - bv['amount']) > 0.005:
                out.append(
                    f'{group} "{_esc(name)}": USD {bv["amount"]:,.2f} '
                    f'→ USD {av["amount"]:,.2f}'
                )
            elif 'qty' in av and av.get('qty') != bv.get('qty'):
                out.append(
                    f'{group} "{_esc(name)}": qty '
                    f'{bv.get("qty", 0):g} → {av.get("qty", 0):g}'
                )
        for name in b:
            if name not in a:
                out.append(
                    f'Removed {group[:-1] if group.endswith("s") else group} '
                    f'"{_esc(name)}"'
                )
    return out


def _is_buffer_log_entry(entry):
    """True when a change_log entry reveals the confidential buffer reserve."""
    if not isinstance(entry, dict):
        return False
    subject = (entry.get('subject') or '').strip()
    body = entry.get('body') or ''
    return subject == BUFFER_LOG_SUBJECT or 'Buffer reserve' in body


def _redact_buffer_for_non_cfo(detail):
    """Remove the confidential buffer data unless the caller is a CFO. Enforces
    the buffer's CFO-only visibility at the API, not just in the UI — this
    covers the buffer fields AND the buffer message in the activity log."""
    if _caller_is_cfo():
        return detail
    for key in _BUFFER_TOPLEVEL_KEYS:
        detail.pop(key, None)
    totals = detail.get('totals')
    if isinstance(totals, dict):
        for key in _BUFFER_TOTALS_KEYS:
            totals.pop(key, None)
    log = detail.get('change_log')
    if isinstance(log, list):
        detail['change_log'] = [
            e for e in log if not _is_buffer_log_entry(e)
        ]
    return detail


def _request_to_detail(req):
    req.ensure_one()
    model_approved_base = sum(req.model_line_ids.mapped('approved_amount'))
    infra_approved_base = sum(req.infra_line_ids.mapped('approved_amount'))
    sub_approved_base = sum(req.subscription_line_ids.mapped('approved_amount'))
    approved_base = (
        model_approved_base + infra_approved_base + sub_approved_base
    )
    model_requested_total = sum(req.model_line_ids.mapped('requested_amount'))
    infra_requested_total = sum(req.infra_line_ids.mapped('requested_amount'))
    sub_requested_total = sum(req.subscription_line_ids.mapped('requested_amount'))
    sub_monthly_total = sum(req.subscription_line_ids.mapped('final_amount'))
    sub_per_day_total = sub_monthly_total / 30.0 if sub_monthly_total else 0.0
    buffer_factor = (req.buffer_pct or 0.0) / 100.0

    def _mshare(amount):
        return _share_pct(amount, approved_base)

    model_lines = [
        {
            'id': ln.id,
            'ai_model_id': ln.ai_model_id.id,
            'ai_model_name': ln.ai_model_name or '',
            'model_name': (
                ln.ai_model_name
                or ln.ai_model_id.display_name
                or ln.ai_model_id.name
            ),
            'usage_tag': (
                ln.usage_tag if 'usage_tag' in ln._fields
                else 'trajectory_building'
            ),
            'description': ln.description if 'description' in ln._fields else '',
            'cost_type': ln.cost_type,
            'per_task_cost': ln.per_task_cost,
            'per_trajectory_cost': ln.per_trajectory_cost,
            'iterations': ln.iterations,
            'requested_amount': ln.requested_amount,
            'approved_amount': ln.approved_amount,
            'approved_share_pct': _mshare(ln.approved_amount or 0.0),
        }
        for ln in req.model_line_ids
    ]
    infra_lines = [
        {
            'id': ln.id,
            'infra_id': ln.infra_type_id.id,
            'infra_name': ln.infra_type_id.display_name or ln.infra_type_id.name,
            'description': ln.description or '',
            'requested_amount': ln.requested_amount,
            'approved_amount': ln.approved_amount,
            'per_day_requested': (
                ln.per_day_requested if 'per_day_requested' in ln._fields
                else (ln.requested_amount or 0.0) / 30.0
            ),
            'per_day_approved': (
                ln.per_day_approved if 'per_day_approved' in ln._fields
                else (ln.approved_amount or 0.0) / 30.0
            ),
            'approved_share_pct': _mshare(ln.approved_amount or 0.0),
            'start_date': ln.start_date.isoformat() if ln.start_date else None,
            'end_date': ln.end_date.isoformat() if ln.end_date else None,
            'instance_type': ln.instance_type or '',
            'unit_price_usd': ln.unit_price_usd or 0.0,
            'price_unit': ln.price_unit or '',
            'quantity': ln.quantity or 0.0,
            'duration_hours': ln.duration_hours or 0.0,
            'ebs_storage_gb': ln.ebs_storage_gb or 0.0,
            'volume_type': ln.volume_type or '',
            'volume_rate_usd_per_gb_mo': ln.volume_rate_usd_per_gb_mo or 0.0,
            'compute_amount': (
                ln.compute_amount if 'compute_amount' in ln._fields else 0.0
            ),
            'storage_amount': (
                ln.storage_amount if 'storage_amount' in ln._fields else 0.0
            ),
            'computed_amount': (
                ln.computed_amount if 'computed_amount' in ln._fields else 0.0
            ),
        }
        for ln in req.infra_line_ids
    ]
    sub_lines = [
        {
            'id': ln.id,
            'subscription_id': ln.subscription_id.id,
            'subscription_name': (
                ln.subscription_id.display_name or ln.subscription_id.name
            ),
            'cost_per_subscription': ln.cost_per_subscription,
            'assigned_to': ln.assigned_user_ids.ids,
            'seat_count': ln.subscription_count,
            'monthly_total': ln.final_amount,
            'per_day_cost': (
                ln.per_day_cost if 'per_day_cost' in ln._fields
                else (ln.final_amount or 0.0) / 30.0
            ),
            'requested_amount': ln.requested_amount,
            'approved_amount': ln.approved_amount,
            'approved_share_pct': _mshare(ln.approved_amount or 0.0),
        }
        for ln in req.subscription_line_ids
    ]

    model_requested_base = model_requested_total
    infra_requested_base = infra_requested_total
    sub_requested_base = sub_requested_total

    detail = _request_to_summary(req)
    attachments = _parse_attachment_links(req.attachment_urls)
    detail.update({
        'justification': req.justification or '',
        'subject': req.subject or '',
        'message': req.message or '',
        'attachment_ids': req.attachment_ids.ids,
        'attachments': (
            [_attachment_brief(a) for a in req.attachment_ids]
            if not attachments else attachments
        ),
        'attachment_urls': attachments,
        'topup_reason_id': (
            req.topup_reason_id.id if req.topup_reason_id else None
        ),
        'topup_reason': (
            _topup_reason_brief(req.topup_reason_id)
            if req.topup_reason_id else None
        ),
        'rejection_reason': req.rejection_reason or '',
        # `rejected_by` / `rejected_by_role` come from `_request_to_summary`.
        'cto_review_note': getattr(req, 'cto_review_note', '') or '',
        'cfo_change_request_note': (
            getattr(req, 'cfo_change_request_note', '') or ''
        ),
        'buffer_pct': req.buffer_pct,
        # Stable CFO "Financial overview" snapshots (see model fields).
        'original_requested_total': req.original_requested_total,
        'cto_forwarded_total': req.cto_forwarded_total,
        'model_lines': model_lines,
        'infra_lines': infra_lines,
        'subscription_lines': sub_lines,
        'change_log': _change_log(req),
        'totals': {
            'model_requested_total': model_requested_base,
            'model_approved_total': model_approved_base,
            'infra_requested_total': infra_requested_base,
            'infra_approved_total': infra_approved_base,
            'subscription_requested_total': sub_requested_base,
            'subscription_approved_total': sub_approved_base,
            'subscription_monthly_total': sub_monthly_total,
            'subscription_per_day_total': sub_per_day_total,
            'buffer_pct': req.buffer_pct,
            # Persisted buffer reserve (stored on the request, kept OUT of
            # approved_total). The *_requested/_approved figures below remain
            # computed for display parity.
            'buffer_amount': req.buffer_amount,
            'buffer_amount_requested': (
                model_requested_total
                + infra_requested_total
                + sub_requested_total
            ) * buffer_factor,
            'buffer_amount_approved': approved_base * buffer_factor,
        },
        'approved_allocation': {
            'model': {
                'amount': model_approved_base,
                'share_pct': _mshare(model_approved_base),
            },
            'infra': {
                'amount': infra_approved_base,
                'share_pct': _mshare(infra_approved_base),
            },
            'subscription': {
                'amount': sub_approved_base,
                'share_pct': _mshare(sub_approved_base),
                'monthly_amount': sub_monthly_total,
                'per_day_amount': sub_per_day_total,
            },
        },
        'project_budget_overview': _budget_overview(req.budget_id),
        'request_batch': {
            'short': _batch_short(req.phase_id),
            'detail': _batch_detail(req.phase_id),
        },
    })
    return _redact_buffer_for_non_cfo(detail)


# ---------------------------------------------------------------------------
# Line-command builders
# ---------------------------------------------------------------------------

def _build_request_model_cmds(entries, total_tasks, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f'model_lines[{idx}] must be an object.'
        ai_model_id = _coerce_int(
            line.get('ai_model_id') or line.get('model_id'),
        )
        if not ai_model_id:
            return [], 0.0, f'model_lines[{idx}].ai_model_id is required.'
        cost_type = line.get('cost_type') or 'per_task'
        if cost_type not in VALID_COST_TYPES:
            return [], 0.0, (
                f'model_lines[{idx}].cost_type must be \'per_task\' or '
                '\'per_trajectory\'.'
            )
        per_task_cost = _coerce_float(line.get('per_task_cost'), 0.0)
        per_traj = _coerce_float(line.get('per_trajectory_cost'), 0.0)
        iterations = _coerce_int(
            line.get('iterations') or line.get('no_of_trajectory'), 0,
        ) or 0
        if cost_type == 'per_trajectory':
            per_task_cost = per_traj * iterations
        if 'requested_amount' in line:
            requested_amount = _coerce_float(line.get('requested_amount'), 0.0)
        else:
            requested_amount = (total_tasks or 0) * per_task_cost
        subtotal += requested_amount
        line_vals = {
            'ai_model_id': ai_model_id,
            'ai_model_name': (line.get('ai_model_name') or '').strip() or False,
            'cost_type': cost_type,
            'per_task_cost': per_task_cost,
            'per_trajectory_cost': per_traj,
            'iterations': iterations,
            'requested_amount': requested_amount,
        }
        if 'approved_amount' in line:
            line_vals['approved_amount'] = _coerce_float(
                line.get('approved_amount'), 0.0,
            )
        cmds.append((0, 0, line_vals))
    if cmds:
        missing = _missing_ids(
            AI_MODEL, [c[2]['ai_model_id'] for c in cmds],
        )
        if missing:
            return [], 0.0, f'Model(s) not found: {missing}'
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _build_request_infra_cmds(entries, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f'infra_lines[{idx}] must be an object.'
        infra_type_id = _coerce_int(
            line.get('infra_type_id') or line.get('infra_id'),
        )
        if not infra_type_id:
            return [], 0.0, f'infra_lines[{idx}].infra_type_id is required.'
        requested_amount = _coerce_float(line.get('requested_amount'), 0.0)
        subtotal += requested_amount
        vals = {
            'infra_type_id': infra_type_id,
            'description': line.get('description') or False,
            'requested_amount': requested_amount,
        }
        sd = _coerce_date(line.get('start_date'))
        ed = _coerce_date(line.get('end_date'))
        if sd:
            vals['start_date'] = sd
        if ed:
            vals['end_date'] = ed
        if 'approved_amount' in line:
            vals['approved_amount'] = _coerce_float(
                line.get('approved_amount'), 0.0,
            )
        # Persist the structured instance specs. Because the update / CTO-forward
        # paths call this with replace=True (delete + recreate every line), the
        # client MUST echo these back or they'd be lost — previously they were
        # silently wiped on every resubmit. Only write keys the client sent so a
        # caller that omits them doesn't zero out an existing value on a new line.
        for key, coerce in (
            ('instance_type', lambda v: v or False),
            ('unit_price_usd', lambda v: _coerce_float(v, 0.0)),
            ('price_unit', lambda v: v or False),
            ('quantity', lambda v: _coerce_float(v, 0.0)),
            ('duration_hours', lambda v: _coerce_float(v, 0.0)),
            ('ebs_storage_gb', lambda v: _coerce_float(v, 0.0)),
            ('volume_type', lambda v: v or False),
            ('volume_rate_usd_per_gb_mo', lambda v: _coerce_float(v, 0.0)),
        ):
            if key in line:
                vals[key] = coerce(line.get(key))
        cmds.append((0, 0, vals))
    if cmds:
        missing = _missing_ids(
            INFRA_MODEL, [c[2]['infra_type_id'] for c in cmds],
        )
        if missing:
            return [], 0.0, f'Infrastructure type(s) not found: {missing}'
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _build_request_subscription_cmds(entries, replace=False):
    cmds = []
    subtotal = 0.0
    for idx, line in enumerate(entries):
        if not isinstance(line, dict):
            return [], 0.0, f'subscription_lines[{idx}] must be an object.'
        subscription_id = _coerce_int(line.get('subscription_id'))
        if not subscription_id:
            return [], 0.0, (
                f'subscription_lines[{idx}].subscription_id is required.'
            )
        assigned = (
            line.get('assigned_user_ids') or line.get('assigned_to') or []
        )
        if not isinstance(assigned, (list, tuple)):
            return [], 0.0, (
                f'subscription_lines[{idx}].assigned_user_ids must be a list.'
            )
        assigned_ids = [_coerce_int(u) for u in assigned if _coerce_int(u)]
        requested_amount = _coerce_float(line.get('requested_amount'), 0.0)
        subtotal += requested_amount
        line_vals = {
            'subscription_id': subscription_id,
            'assigned_user_ids': [(6, 0, assigned_ids)],
            'requested_amount': requested_amount,
        }
        sd = _coerce_date(line.get('start_date'))
        ed = _coerce_date(line.get('end_date'))
        if sd:
            line_vals['start_date'] = sd
        if ed:
            line_vals['end_date'] = ed
        if 'approved_amount' in line:
            line_vals['approved_amount'] = _coerce_float(
                line.get('approved_amount'), 0.0,
            )
        cmds.append((0, 0, line_vals))
    if cmds:
        missing = _missing_ids(
            SUBSCRIPTION_MODEL, [c[2]['subscription_id'] for c in cmds],
        )
        if missing:
            return [], 0.0, f'Subscription(s) not found: {missing}'
    if replace:
        return [(5, 0, 0)] + cmds, subtotal, None
    return cmds, subtotal, None


def _apply_line_overrides(req, jdata):
    for key, attr in (
        ('model_lines', 'model_line_ids'),
        ('infra_lines', 'infra_line_ids'),
        ('subscription_lines', 'subscription_line_ids'),
    ):
        for line in jdata.get(key) or []:
            if not isinstance(line, dict):
                continue
            line_id = _coerce_int(line.get('id'))
            if not line_id or 'approved_amount' not in line:
                continue
            target = getattr(req, attr).filtered(lambda l: l.id == line_id)
            if not target:
                continue
            write_vals = {
                'approved_amount': _coerce_float(
                    line.get('approved_amount'), 0.0,
                ),
            }
            # The CFO may edit an infra line's quantity in place (the only
            # structured field they can change). The compute/storage/computed
            # amounts recompute automatically from quantity on the line model.
            if attr == 'infra_line_ids' and 'quantity' in line:
                write_vals['quantity'] = _coerce_float(line.get('quantity'), 0.0)
            target.write(write_vals)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class EtharaBudgetRequestController(http.Controller):
    """HTTP endpoints for the phase budget request/approval workflow."""

    # ---------------------------------------------------------------- list
    @http.route(
        '/api/v1/ethara_project/budget/requests/list',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def list_budget_requests(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Request = request.env[REQUEST_MODEL].sudo()
        domain = []
        project_id = _coerce_int(params.get('project_id'))
        if project_id:
            domain.append(('ethara_project_id', '=', project_id))
        # Flutter sends `batch_id`; resolve as the phase FK.
        batch_id = _coerce_int(params.get('batch_id'))
        if batch_id:
            domain.append(('phase_id', '=', batch_id))
        project_budget_id = _coerce_int(params.get('project_budget_id'))
        if project_budget_id:
            domain.append(('budget_id', '=', project_budget_id))
        state = (params.get('state') or '').strip()
        if state:
            state_aliases = {
                'pending': ['draft', 'cto_review', 'cfo_review'],
                'approved': ['approved', 'partially_approved'],
                # A CTO send-back / CFO return writes state=changes_required; a
                # terminal CFO reject writes state=rejected. Both read as
                # "rejected" in the queue's Rejected filter.
                'rejected': ['changes_required', 'rejected'],
                'withdrawn': ['withdrawn'],
            }
            valid_states = [v for v, _l in Request._fields['state'].selection]
            if state in state_aliases:
                domain.append(('state', 'in', state_aliases[state]))
            elif state in valid_states:
                domain.append(('state', '=', state))
            else:
                return return_Response(
                    message=(
                        f'state must be one of {list(state_aliases.keys())} '
                        f'(aliases) or {valid_states} (raw states).'
                    ),
                    status=400, data={},
                )
        request_type = (params.get('request_type') or '').strip()
        if request_type and 'request_type' in Request._fields:
            valid_types = [
                v for v, _l in Request._fields['request_type'].selection
            ]
            if request_type not in valid_types:
                return return_Response(
                    message=f'request_type must be one of {valid_types}.',
                    status=400, data={},
                )
            domain.append(('request_type', '=', request_type))
        cto_reviewer_id = _coerce_int(params.get('cto_reviewer_id'))
        if cto_reviewer_id and 'cto_reviewer_id' in Request._fields:
            domain.append(('cto_reviewer_id', '=', cto_reviewer_id))
        cfo_approver_id = _coerce_int(params.get('cfo_approver_id'))
        if cfo_approver_id and 'cfo_approver_id' in Request._fields:
            domain.append(('cfo_approver_id', '=', cfo_approver_id))
        requester_id = _coerce_int(params.get('requester_id'))
        if requester_id:
            domain.append(('requester_id', '=', requester_id))
        search = (params.get('search') or '').strip()
        if search:
            domain += [
                '|', ('name', 'ilike', search),
                ('justification', 'ilike', search),
            ]
        total = Request.search_count(domain)
        records = Request.search(
            domain, limit=limit, offset=offset,
            order='request_date desc, id desc',
        )
        items = [_request_to_summary(r) for r in records]
        return return_Response(
            message='Budget requests fetched.', status=200,
            data={
                'total': total, 'limit': limit, 'offset': offset,
                'requests': items,
            },
        )

    # -------------------------------------------------------------- detail
    @http.route(
        '/api/v1/ethara_project/budget/requests/detail',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def budget_request_detail(self, **params):
        req_id = _coerce_int(params.get('id'))
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        rec = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not rec:
            return return_Response(
                message='Budget request not found.', status=404, data={},
            )
        try:
            payload = _request_to_detail(rec)
            # Build the response INSIDE the try so a JSON-encoding error (e.g. a
            # stray non-serializable value) degrades to a clean 400, not a raw
            # HTML 500 from the WSGI layer.
            return return_Response(
                message='Budget request detail fetched.', status=200,
                data={'data': payload},
            )
        except Exception as e:
            _logger.exception(
                'budget_request_detail serialization failed for id=%s state=%s',
                rec.id, rec.state,
            )
            return return_Response(
                message=f'Failed to serialize budget request: {e}',
                status=400, data={'errors': [str(e)]},
            )

    # -------------------------------------------------------------- create
    @http.route(
        '/api/v1/ethara_project/budget/requests/create',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def create_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        Request = request.env[REQUEST_MODEL].sudo()
        Phase = request.env[PHASE_MODEL].sudo()

        # Flutter sends `batch_id`; resolve as the phase id.
        batch_id = _coerce_int(jdata.get('batch_id'))
        if not batch_id:
            return return_Response(
                message='batch_id is required.', status=400, data={},
            )
        phase = Phase.browse(batch_id).exists()
        if not phase:
            return return_Response(
                message=f'Phase {batch_id} not found.', status=404, data={},
            )

        request_type = (jdata.get('request_type') or 'budget').strip()
        valid_types = [v for v, _l in Request._fields['request_type'].selection]
        if request_type not in valid_types:
            return return_Response(
                message=f'request_type must be one of {valid_types}.',
                status=400, data={},
            )

        justification = (jdata.get('justification') or '').strip()
        if not justification:
            return return_Response(
                message='justification is required.', status=400, data={},
            )

        priority = (jdata.get('priority') or 'normal').strip()
        if priority not in VALID_PRIORITIES:
            return return_Response(
                message='priority must be one of low, normal, high, urgent.',
                status=400, data={},
            )

        total_tasks = _coerce_int(jdata.get('total_tasks'), 0) or 0
        buffer_pct = _coerce_float(jdata.get('buffer_pct'), 0.0)

        model_cmds, model_sub, err = _build_request_model_cmds(
            jdata.get('model_lines') or [], total_tasks,
        )
        if err:
            return return_Response(message=err, status=400, data={})
        infra_cmds, infra_sub, err = _build_request_infra_cmds(
            jdata.get('infra_lines') or [],
        )
        if err:
            return return_Response(message=err, status=400, data={})
        sub_cmds, sub_sub, err = _build_request_subscription_cmds(
            jdata.get('subscription_lines') or [],
        )
        if err:
            return return_Response(message=err, status=400, data={})

        if 'requested_total' in jdata:
            requested_total = _coerce_float(jdata.get('requested_total'), 0.0)
        else:
            subtotal = model_sub + infra_sub + sub_sub
            requested_total = subtotal * (1.0 + (buffer_pct / 100.0))

        vals = {
            'phase_id': phase.id,
            'request_type': request_type,
            'justification': justification,
            'subject': jdata.get('subject') or False,
            'message': jdata.get('message') or False,
            'priority': priority,
            'total_tasks': total_tasks,
            'buffer_pct': buffer_pct,
            'requested_total': requested_total,
            'model_line_ids': model_cmds,
            'infra_line_ids': infra_cmds,
            'subscription_line_ids': sub_cmds,
        }

        raw_attachments = jdata.get('attachment_ids') or []
        raw_attachment_ids = []
        if raw_attachments:
            if not isinstance(raw_attachments, (list, tuple)):
                return return_Response(
                    message='attachment_ids must be a list of integers.',
                    status=400, data={},
                )
            raw_attachment_ids = [
                _coerce_int(x) for x in raw_attachments if _coerce_int(x)
            ]
            missing = _missing_ids('ir.attachment', raw_attachment_ids)
            if missing:
                return return_Response(
                    message=f'Attachment(s) not found: {missing}',
                    status=400, data={},
                )

        reason_id = _coerce_int(jdata.get('topup_reason_id'))
        if reason_id:
            if _missing_ids(TOPUP_REASON_MODEL, [reason_id]):
                return return_Response(
                    message=f'Top-up reason {reason_id} not found.',
                    status=400, data={},
                )
            vals['topup_reason_id'] = reason_id

        try:
            req = Request.create(vals)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('create_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )

        linked_attachment_ids = list(raw_attachment_ids)
        persisted_urls = []
        if raw_attachment_ids:
            for att in request.env['ir.attachment'].sudo().browse(
                raw_attachment_ids
            ):
                if att.type == 'url' and att.url:
                    persisted_urls.append(att.url)

        if uploaded_files:
            try:
                new_att_ids, new_urls = _upload_files_as_url_attachments(
                    uploaded_files, REQUEST_MODEL, req.id,
                )
                linked_attachment_ids.extend(new_att_ids)
                persisted_urls.extend(new_urls)
            except Exception:
                _logger.exception('uploaded attachment persistence failed')

        if linked_attachment_ids:
            write_vals = {'attachment_ids': [(6, 0, linked_attachment_ids)]}
            if 'attachment_urls' in req._fields:
                write_vals['attachment_urls'] = (
                    _serialize_attachment_links(persisted_urls) or False
                )
            req.sudo().with_context(
                tracking_disable=True, mail_notrack=True,
            ).write(write_vals)

        submit_flag = jdata.get('submit')
        if submit_flag is None:
            submit_flag = True
        if submit_flag:
            try:
                req.action_submit_for_approval()
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, data={'request_id': req.id},
                )
            except Exception as e:
                _logger.exception('submit on create failed')
                return return_Response(
                    message='Created but submit failed.', status=400,
                    data={'request_id': req.id, 'errors': [str(e)]},
                )

        return return_Response(
            message='Budget request created.', status=200,
            data={'data': _request_to_detail(req.sudo())},
        )

    # -------------------------------------------------------------- update
    @http.route(
        '/api/v1/ethara_project/budget/requests/update',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def update_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get('id'))
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        req = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message=f'Budget request {req_id} not found.',
                status=404, data={},
            )
        if req.state not in ('draft', 'changes_required'):
            return return_Response(
                message=(
                    f'Cannot edit request in state \'{req.state}\'. Only draft '
                    'and changes_required are editable.'
                ),
                status=400, data={},
            )

        vals = {}

        if 'request_type' in jdata:
            rt = (jdata.get('request_type') or '').strip()
            valid_types = [
                v for v, _l in req._fields['request_type'].selection
            ]
            if rt not in valid_types:
                return return_Response(
                    message=f'request_type must be one of {valid_types}.',
                    status=400, data={},
                )
            vals['request_type'] = rt

        if 'justification' in jdata:
            justification = (jdata.get('justification') or '').strip()
            if not justification:
                return return_Response(
                    message='justification cannot be empty.',
                    status=400, data={},
                )
            vals['justification'] = justification

        if 'subject' in jdata:
            vals['subject'] = jdata.get('subject') or False
        if 'message' in jdata:
            vals['message'] = jdata.get('message') or False

        if 'priority' in jdata:
            priority = (jdata.get('priority') or 'normal').strip()
            if priority not in VALID_PRIORITIES:
                return return_Response(
                    message=(
                        'priority must be one of low, normal, high, urgent.'
                    ),
                    status=400, data={},
                )
            vals['priority'] = priority

        if 'total_tasks' in jdata:
            vals['total_tasks'] = _coerce_int(jdata.get('total_tasks'), 0) or 0
        if 'buffer_pct' in jdata:
            vals['buffer_pct'] = _coerce_float(jdata.get('buffer_pct'), 0.0)

        total_tasks_eff = vals.get('total_tasks', req.total_tasks)

        if 'model_lines' in jdata:
            cmds, _sub, err = _build_request_model_cmds(
                jdata.get('model_lines') or [], total_tasks_eff, replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals['model_line_ids'] = cmds
        if 'infra_lines' in jdata:
            cmds, _sub, err = _build_request_infra_cmds(
                jdata.get('infra_lines') or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals['infra_line_ids'] = cmds
        if 'subscription_lines' in jdata:
            cmds, _sub, err = _build_request_subscription_cmds(
                jdata.get('subscription_lines') or [], replace=True,
            )
            if err:
                return return_Response(message=err, status=400, data={})
            vals['subscription_line_ids'] = cmds

        if 'requested_total' in jdata:
            vals['requested_total'] = _coerce_float(
                jdata.get('requested_total'), 0.0,
            )

        if 'attachment_ids' in jdata:
            raw = jdata.get('attachment_ids') or []
            if not isinstance(raw, (list, tuple)):
                return return_Response(
                    message='attachment_ids must be a list of integers.',
                    status=400, data={},
                )
            ids = [_coerce_int(x) for x in raw if _coerce_int(x)]
            missing = _missing_ids('ir.attachment', ids)
            if missing:
                return return_Response(
                    message=f'Attachment(s) not found: {missing}',
                    status=400, data={},
                )
            vals['attachment_ids'] = [(6, 0, ids)]

        if 'topup_reason_id' in jdata:
            raw = jdata.get('topup_reason_id')
            if raw in (None, '', 0, False):
                vals['topup_reason_id'] = False
            else:
                reason_id = _coerce_int(raw)
                if not reason_id or _missing_ids(
                    TOPUP_REASON_MODEL, [reason_id]
                ):
                    return return_Response(
                        message=f'Top-up reason {raw} not found.',
                        status=400, data={},
                    )
                vals['topup_reason_id'] = reason_id

        # Snapshot the lines BEFORE the write (they're replaced wholesale) so we
        # can log what the raiser changed.
        _before_snap = _request_line_snapshot(req)
        _before_total = req.requested_total or 0.0
        _before_just = req.justification or ''

        try:
            if vals:
                req.write(vals)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('update_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )

        # Log WHAT changed and WHO changed it into the chatter → surfaces in the
        # detail page's activity log (`_change_log`). Never let logging break the
        # update.
        if vals:
            try:
                changes = _diff_request_lines(
                    _before_snap, _request_line_snapshot(req),
                )
                _after_total = req.requested_total or 0.0
                if abs(_after_total - _before_total) > 0.005:
                    changes.insert(
                        0,
                        f'Requested total: USD {_before_total:,.2f} '
                        f'→ USD {_after_total:,.2f}',
                    )
                if (req.justification or '') != _before_just:
                    changes.append('Justification updated.')
                if changes:
                    body = (
                        f'<strong>{RESUBMIT_LOG_SUBJECT}</strong><br/>'
                        f'By: {_esc(request.env.user.display_name)}<br/>'
                        + '<br/>'.join('&bull; ' + c for c in changes)
                    )
                    req.message_post(body=body, subject=RESUBMIT_LOG_SUBJECT)
            except Exception:
                _logger.exception('update_budget_request change-log failed')

        if uploaded_files:
            new_att_ids, _urls = _upload_files_as_url_attachments(
                uploaded_files, REQUEST_MODEL, req.id,
            )
            if new_att_ids:
                req.sudo().write({
                    'attachment_ids': [(4, i, 0) for i in new_att_ids],
                })

        if 'attachment_ids' in jdata or uploaded_files:
            _sync_attachment_urls_from_ids(req.sudo())

        if jdata.get('submit'):
            try:
                req.action_submit_for_approval()
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, data={'request_id': req.id},
                )
            except Exception as e:
                _logger.exception('submit on update failed')
                return return_Response(
                    message='Updated but submit failed.', status=400,
                    data={'request_id': req.id, 'errors': [str(e)]},
                )

        return return_Response(
            message='Budget request updated.', status=200,
            data={'data': _request_to_detail(req.sudo())},
        )

    # ------------------------------------------------------------- approve
    @http.route(
        '/api/v1/ethara_project/budget/requests/approve',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def approve_budget_request(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get('id'))
        step = (jdata.get('step') or '').strip().lower()
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        if step not in ('cto', 'cfo'):
            return return_Response(
                message='step must be \'cto\' or \'cfo\'.', status=400, data={},
            )

        req = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message=f'Budget request {req_id} not found.',
                status=404, data={},
            )

        if step == 'cto' and req.state != 'cto_review':
            return return_Response(
                message=f'Cannot CTO-approve request in state \'{req.state}\'.',
                status=400, data={},
            )
        if step == 'cfo' and req.state != 'cfo_review':
            return return_Response(
                message=f'Cannot CFO-approve request in state \'{req.state}\'.',
                status=400, data={},
            )

        if 'request_type' in jdata:
            rt = (jdata.get('request_type') or '').strip()
            if rt and rt != req.request_type:
                return return_Response(
                    message=(
                        f'request_type cannot be changed during approval '
                        f'(current=\'{req.request_type}\'). Send back for '
                        'changes if a type change is needed.'
                    ),
                    status=400, data={},
                )

        vals = {}
        if 'justification' in jdata:
            justification = (jdata.get('justification') or '').strip()
            if not justification:
                return return_Response(
                    message='justification cannot be empty.',
                    status=400, data={},
                )
            vals['justification'] = justification
        if 'subject' in jdata:
            vals['subject'] = jdata.get('subject') or False
        if 'message' in jdata:
            vals['message'] = jdata.get('message') or False
        if 'priority' in jdata:
            priority = (jdata.get('priority') or 'normal').strip()
            if priority not in VALID_PRIORITIES:
                return return_Response(
                    message=(
                        'priority must be one of low, normal, high, urgent.'
                    ),
                    status=400, data={},
                )
            vals['priority'] = priority
        if 'total_tasks' in jdata:
            vals['total_tasks'] = _coerce_int(jdata.get('total_tasks'), 0) or 0
        if 'buffer_pct' in jdata:
            vals['buffer_pct'] = _coerce_float(jdata.get('buffer_pct'), 0.0)

        total_tasks_eff = vals.get('total_tasks', req.total_tasks)

        # The line arrays REPLACE the request's lines (with new requested
        # amounts) only at the CTO step, where the CTO forwards a revised
        # request. At the CFO step the same keys carry per-line APPROVED-amount
        # overrides ({id, approved_amount}) applied by `_apply_line_overrides`
        # below — so skip the replace block for CFO to avoid clobbering line ids
        # (and rejecting override rows that omit ai_model_id / infra_type_id).
        if step == 'cto':
            if 'model_lines' in jdata:
                cmds, _sub, err = _build_request_model_cmds(
                    jdata.get('model_lines') or [], total_tasks_eff,
                    replace=True,
                )
                if err:
                    return return_Response(message=err, status=400, data={})
                vals['model_line_ids'] = cmds
            if 'infra_lines' in jdata:
                cmds, _sub, err = _build_request_infra_cmds(
                    jdata.get('infra_lines') or [], replace=True,
                )
                if err:
                    return return_Response(message=err, status=400, data={})
                vals['infra_line_ids'] = cmds
            if 'subscription_lines' in jdata:
                cmds, _sub, err = _build_request_subscription_cmds(
                    jdata.get('subscription_lines') or [], replace=True,
                )
                if err:
                    return return_Response(message=err, status=400, data={})
                vals['subscription_line_ids'] = cmds

        if 'requested_total' in jdata:
            vals['requested_total'] = _coerce_float(
                jdata.get('requested_total'), 0.0,
            )

        if 'attachment_ids' in jdata:
            raw = jdata.get('attachment_ids') or []
            if not isinstance(raw, (list, tuple)):
                return return_Response(
                    message='attachment_ids must be a list of integers.',
                    status=400, data={},
                )
            ids = [_coerce_int(x) for x in raw if _coerce_int(x)]
            missing = _missing_ids('ir.attachment', ids)
            if missing:
                return return_Response(
                    message=f'Attachment(s) not found: {missing}',
                    status=400, data={},
                )
            vals['attachment_ids'] = [(6, 0, ids)]

        try:
            if vals:
                req.write(vals)
            if step == 'cto':
                req.action_cto_approve()
                _apply_line_overrides(req, jdata)
                if 'approved_total' in jdata:
                    req.write({
                        'approved_total': _coerce_float(
                            jdata.get('approved_total'), 0.0,
                        ),
                    })
                if 'approved_amount' in jdata:
                    req.write({
                        'approved_total': _coerce_float(
                            jdata.get('approved_amount'), 0.0,
                        ),
                    })
            else:
                _apply_line_overrides(req, jdata)
                if 'approved_total' in jdata:
                    req.write({
                        'approved_total': _coerce_float(
                            jdata.get('approved_total'), 0.0,
                        ),
                    })
                if 'approved_amount' in jdata:
                    # The client sends the BASE approved amount here (buffer is
                    # NOT folded in); it becomes approved_total verbatim.
                    req.write({
                        'approved_total': _coerce_float(
                            jdata.get('approved_amount'), 0.0,
                        ),
                    })
                # Per-line override mode: the CFO edited individual model /
                # subscription line amounts (infra stays locked). When those
                # arrays are present the per-line `approved_amount` figures are
                # authoritative — `action_cfo_approve` recomputes the total from
                # the lines and skips auto-redistribution.
                cfo_line_override = (
                    'model_lines' in jdata
                    or 'subscription_lines' in jdata
                    or 'infra_lines' in jdata
                )
                ctx = {}
                if cfo_line_override:
                    ctx['cfo_line_override'] = True
                # Explicit Approve (full) vs Partial decision from the CFO. When
                # `is_partial` is absent, the model infers it from the amounts.
                if 'is_partial' in jdata:
                    ctx['force_partial'] = _coerce_bool(jdata.get('is_partial'))
                req.with_context(**ctx).action_cfo_approve()
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('approve_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )

        if uploaded_files:
            new_att_ids, _urls = _upload_files_as_url_attachments(
                uploaded_files, REQUEST_MODEL, req.id,
            )
            if new_att_ids:
                req.sudo().write({
                    'attachment_ids': [(4, i, 0) for i in new_att_ids],
                })

        if 'attachment_ids' in jdata or uploaded_files:
            _sync_attachment_urls_from_ids(req.sudo())

        return return_Response(
            message=f'Budget request {step.upper()}-approved.', status=200,
            data={'data': _request_to_detail(req.sudo())},
        )

    # -------------------------------------------------------------- reject
    @http.route(
        '/api/v1/ethara_project/budget/requests/reject',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def reject_budget_request(self, **params):
        jdata, _files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get('id'))
        step = (jdata.get('step') or '').strip().lower()
        note = (jdata.get('note') or '').strip()
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        if step not in ('cto', 'cfo'):
            return return_Response(
                message='step must be \'cto\' or \'cfo\'.', status=400, data={},
            )
        if not note:
            return return_Response(
                message='note is required.', status=400, data={},
            )

        req = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message=f'Budget request {req_id} not found.',
                status=404, data={},
            )

        try:
            if step == 'cto':
                if req.state != 'cto_review':
                    return return_Response(
                        message=f'Cannot CTO-reject in state \'{req.state}\'.',
                        status=400, data={},
                    )
                req._do_cto_reject(note)
            else:
                if req.state != 'cfo_review':
                    return return_Response(
                        message=(
                            f'Cannot CFO-request-changes in state '
                            f'\'{req.state}\'.'
                        ),
                        status=400, data={},
                    )
                # `mode='reject'` closes the request terminally (state
                # `rejected`); the default `changes` sends it back to the PL for
                # revision (state `changes_required`).
                mode = (jdata.get('mode') or 'changes').strip().lower()
                if mode == 'reject':
                    req._do_cfo_reject(note)
                else:
                    req._do_cfo_request_changes(note)
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('reject_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )

        if step == 'cto':
            verb = 'CTO send-back'
        elif (jdata.get('mode') or 'changes').strip().lower() == 'reject':
            verb = 'CFO rejection'
        else:
            verb = 'CFO change request'
        return return_Response(
            message=f'Budget request returned with {verb}.', status=200,
            data={'data': _request_to_detail(req.sudo())},
        )

    # --------------------------------------------------------- mark_review
    @http.route(
        '/api/v1/ethara_project/budget/requests/mark_review',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def mark_review_budget_request(self, **params):
        jdata, _files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get('id'))
        step = (jdata.get('step') or '').strip().lower()
        note = (jdata.get('note') or '').strip()
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        if step not in ('cto', 'cfo'):
            return return_Response(
                message='step must be \'cto\' or \'cfo\'.', status=400, data={},
            )
        req = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message='Budget request not found.', status=404, data={},
            )
        if step == 'cto' and req.state != 'cto_review':
            return return_Response(
                message=(
                    f'Cannot mark CTO review in state \'{req.state}\'. '
                    'Request must be in cto_review.'
                ),
                status=400, data={},
            )
        if step == 'cfo' and req.state != 'cfo_review':
            return return_Response(
                message=(
                    f'Cannot mark CFO review in state \'{req.state}\'. '
                    'Request must be in cfo_review.'
                ),
                status=400, data={},
            )
        now = fields.Datetime.now()
        vals = {}
        if step == 'cto':
            vals['cto_reviewer_id'] = request.env.user.id
            vals['cto_review_date'] = now
            if note:
                vals['cto_review_note'] = note
        else:
            vals['cfo_approver_id'] = request.env.user.id
            vals['cfo_approval_date'] = now
            if note:
                vals['cfo_change_request_note'] = note
        try:
            req.write(vals)
            body_lines = [
                f'<strong>{step.upper()} review acknowledged</strong>',
                f'Reviewer: {request.env.user.display_name}',
            ]
            if note:
                body_lines.append(f'Note: {note}')
            req.message_post(
                body='<br/>'.join(body_lines),
                subject=f'{step.upper()} review acknowledged',
                subtype_xmlid='mail.mt_note',
                message_type='notification',
                partner_ids=[],
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('mark_review_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )
        try:
            payload = _request_to_detail(req)
        except Exception as e:
            _logger.exception(
                'mark_review serialization failed for id=%s state=%s',
                req.id, req.state,
            )
            return return_Response(
                message=(
                    f'{step.upper()} review saved but response payload could '
                    f'not be built: {e}'
                ),
                status=400, data={'errors': [str(e)]},
            )
        return return_Response(
            message=f'{step.upper()} review noted.', status=200,
            data={'data': payload},
        )

    @http.route(
        '/api/v1/ethara_project/budget/requests/set_buffer',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def set_buffer_budget_request(self, **params):
        """Persist the CFO's confidential buffer reserve on a request under CFO
        review — WITHOUT approving it. Stores `buffer_pct` and the derived
        `buffer_amount` (a % of the current approved line total). CFO-only; the
        buffer stays redacted for every other role (see `_request_to_detail`)."""
        jdata, _files = _read_multipart_or_json()
        req_id = _coerce_int(jdata.get('id'))
        if not req_id:
            return return_Response(
                message='id is required.', status=400, data={},
            )
        if 'buffer_pct' not in jdata:
            return return_Response(
                message='buffer_pct is required.', status=400, data={},
            )
        pct = _coerce_float(jdata.get('buffer_pct'), 0.0)
        if pct < 0.0 or pct > 100.0:
            return return_Response(
                message='buffer_pct must be between 0 and 100.',
                status=400, data={},
            )
        # The buffer is CFO-confidential — only the CFO may allocate it.
        if not _caller_is_cfo():
            return return_Response(
                message='Only the CFO can allocate the confidential buffer.',
                status=403, data={},
            )
        req = request.env[REQUEST_MODEL].sudo().browse(req_id).exists()
        if not req:
            return return_Response(
                message='Budget request not found.', status=404, data={},
            )
        if req.state != 'cfo_review':
            return return_Response(
                message=(
                    'Buffer can only be allocated while the request is in CFO '
                    f'review (state \'{req.state}\').'
                ),
                status=400, data={},
            )
        # Base = the current CFO working total (sum of line approved amounts),
        # falling back to the requested total. Matches the Flutter card and the
        # figure `action_cfo_approve` finalises the buffer on at approval.
        base = (
            sum(req.model_line_ids.mapped('approved_amount'))
            + sum(req.infra_line_ids.mapped('approved_amount'))
            + sum(req.subscription_line_ids.mapped('approved_amount'))
        ) or (req.requested_total or 0.0)
        amount = base * pct / 100.0
        try:
            req.write({'buffer_pct': pct, 'buffer_amount': amount})
            req.message_post(
                body=(
                    f'<strong>{BUFFER_LOG_SUBJECT}</strong><br/>'
                    f'Reserved {pct:.2f}% (USD {amount:,.2f}) by '
                    f'{request.env.user.display_name}'
                ),
                subject=BUFFER_LOG_SUBJECT,
            )
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400, data={})
        except Exception as e:
            _logger.exception('set_buffer_budget_request failed')
            return return_Response(
                message='Something went wrong.', status=400,
                data={'errors': [str(e)]},
            )
        try:
            payload = _request_to_detail(req)
        except Exception as e:
            _logger.exception(
                'set_buffer serialization failed for id=%s', req.id,
            )
            return return_Response(
                message=f'Buffer saved but response could not be built: {e}',
                status=400, data={'errors': [str(e)]},
            )
        return return_Response(
            message='Buffer reserve saved.', status=200,
            data={'data': payload},
        )
