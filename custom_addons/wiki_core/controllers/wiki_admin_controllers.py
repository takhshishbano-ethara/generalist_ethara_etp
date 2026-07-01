"""Admin-facing CRUD API for wiki content models.

The app-facing read endpoints live in ``wiki_controllers.py`` and return
hand-shaped payloads per screen. This module adds the *management* layer:
flat create / read / update / delete over every wiki content model, plus
owner-scoped write access to grievances and leave.

Content mutations (FAQs, holidays, articles, training, process flows,
categories, updates) are gated behind ``base.group_system`` — normal
employees get a clean 403. Grievance and leave writes are owner-scoped.

All endpoints share the standard envelope from ``utility.return_Response``
and require the ``access_token`` header (``@validate_token``).
"""

import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from .wiki_controllers import _ok, _read_params

_logger = logging.getLogger(__name__)


# ── Resource registry ────────────────────────────────────────────────
# Each resource maps a URL key to its model, an ordered field whitelist
# (type + required + choices/relation metadata) and a default sort order.
# The generic CRUD engine below is driven entirely by this table, so a
# new content type is one entry — no new method.
RESOURCES = {
    'categories': {
        'model': 'wiki.category',
        'order': 'sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'description': {'type': 'char'},
            'icon': {'type': 'char'},
            'route_key': {'type': 'char'},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    'updates': {
        'model': 'wiki.update',
        'order': 'date desc, id desc',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'owner': {'type': 'char'},
            'date': {'type': 'date', 'required': True},
            'active': {'type': 'bool'},
        },
    },
    'faqs': {
        'model': 'wiki.faq',
        'order': 'group_sequence, sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'answer': {'type': 'text', 'required': True},
            'group': {'type': 'char'},
            'group_sequence': {'type': 'int'},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    # Holidays are no longer managed in the Wiki: they are sourced directly
    # from Time Off → Configuration → Public Holidays (single source of
    # truth), so there is no admin CRUD resource for them here.
    'training_groups': {
        'model': 'wiki.training.group',
        'order': 'sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    'training_docs': {
        'model': 'wiki.training.doc',
        'order': 'sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'group_id': {'type': 'many2one', 'required': True,
                         'comodel': 'wiki.training.group'},
            'meta': {'type': 'char'},
            'body': {'type': 'text'},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    'articles': {
        'model': 'wiki.article',
        'order': 'sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'slug': {'type': 'char', 'required': True},
            'description': {'type': 'char'},
            'icon': {'type': 'char'},
            'route_key': {'type': 'char'},
            'meta': {'type': 'char'},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    'article_sections': {
        'model': 'wiki.article.section',
        'order': 'sequence, id',
        'fields': {
            'article_id': {'type': 'many2one', 'required': True,
                           'comodel': 'wiki.article'},
            'heading': {'type': 'char', 'required': True},
            'body': {'type': 'text'},
            'sequence': {'type': 'int'},
        },
    },
    'process_flows': {
        'model': 'wiki.process.flow',
        'order': 'sequence, id',
        'fields': {
            'name': {'type': 'char', 'required': True},
            'meta': {'type': 'char'},
            'sequence': {'type': 'int'},
            'active': {'type': 'bool'},
        },
    },
    'process_stages': {
        'model': 'wiki.process.stage',
        'order': 'sequence, id',
        'fields': {
            'flow_id': {'type': 'many2one', 'required': True,
                        'comodel': 'wiki.process.flow'},
            'name': {'type': 'char', 'required': True},
            'kind': {'type': 'selection', 'required': True,
                     'choices': ['step', 'decision', 'outcome']},
            'sequence': {'type': 'int'},
        },
    },
}

_TRUTHY = {'1', 'true', 'yes', 'on'}
_FALSY = {'0', 'false', 'no', 'off', ''}


def _is_admin():
    return request.env.user.has_group('base.group_system')


def _coerce(value, meta):
    """Coerce/validate one incoming value against its field spec.

    Returns (ok, coerced_value_or_error_message).
    """
    t = meta['type']
    if t in ('char', 'text'):
        return True, '' if value is None else str(value)
    if t == 'int':
        try:
            return True, int(value)
        except (TypeError, ValueError):
            return False, 'must be an integer'
    if t == 'bool':
        if isinstance(value, bool):
            return True, value
        s = str(value).strip().lower()
        if s in _TRUTHY:
            return True, True
        if s in _FALSY:
            return True, False
        return False, 'must be a boolean'
    if t == 'date':
        from datetime import datetime
        try:
            datetime.strptime(str(value), '%Y-%m-%d')
            return True, str(value)
        except (TypeError, ValueError):
            return False, 'must be a date (YYYY-MM-DD)'
    if t == 'selection':
        if value in meta.get('choices', []):
            return True, value
        return False, 'must be one of %s' % ', '.join(meta.get('choices', []))
    if t == 'many2one':
        try:
            rid = int(value)
        except (TypeError, ValueError):
            return False, 'must be a record id'
        if not request.env[meta['comodel']].sudo().browse(rid).exists():
            return False, 'references a missing %s' % meta['comodel']
        return True, rid
    return True, value


def _build_vals(spec, params, partial):
    """Validate params against the field spec.

    partial=False (create) enforces required fields; partial=True (update)
    only validates fields that were actually supplied.
    Returns (vals_dict, errors_list).
    """
    vals, errors = {}, []
    for fname, meta in spec['fields'].items():
        present = fname in params
        if not present:
            if meta.get('required') and not partial:
                errors.append("'%s' is required" % fname)
            continue
        raw = params.get(fname)
        if meta.get('required') and (raw is None or raw == ''):
            errors.append("'%s' cannot be empty" % fname)
            continue
        ok, result = _coerce(raw, meta)
        if not ok:
            errors.append("'%s' %s" % (fname, result))
        else:
            vals[fname] = result
    return vals, errors


def _serialize(rec, spec):
    out = {'id': rec.id}
    for fname, meta in spec['fields'].items():
        val = rec[fname]
        t = meta['type']
        if t == 'date':
            out[fname] = val.isoformat() if val else ''
        elif t == 'many2one':
            out[fname] = (val.id if val else False)
            out['%s_name' % fname[:-3] if fname.endswith('_id') else fname] = (
                val.display_name if val else '')
        elif t == 'int':
            out[fname] = int(val) if val else 0
        elif t == 'bool':
            out[fname] = bool(val)
        else:
            out[fname] = val or ''
    return out


class WikiAdminController(http.Controller):

    # ── Generic content CRUD ─────────────────────────────────────────
    @http.route('/api/v1/wiki/admin/<string:resource>', type='http',
                auth='none', methods=['GET', 'POST'], csrf=False, cors='*')
    @validate_token
    def admin_collection(self, resource, **kwargs):
        spec = RESOURCES.get(resource)
        if not spec:
            return return_Response(message='Unknown resource: %s' % resource,
                                   status=404)
        if request.httprequest.method == 'POST':
            return self._create(spec)
        return self._list(spec)

    @http.route('/api/v1/wiki/admin/<string:resource>/<int:rid>', type='http',
                auth='none', methods=['GET', 'PUT', 'DELETE'], csrf=False,
                cors='*')
    @validate_token
    def admin_member(self, resource, rid, **kwargs):
        spec = RESOURCES.get(resource)
        if not spec:
            return return_Response(message='Unknown resource: %s' % resource,
                                   status=404)
        method = request.httprequest.method
        if method == 'PUT':
            return self._update(spec, rid)
        if method == 'DELETE':
            return self._delete(spec, rid)
        return self._read_one(spec, rid)

    # ── CRUD primitives ──────────────────────────────────────────────
    def _list(self, spec):
        params = _read_params()
        domain = []
        # Default: active records only. ?active=all → include archived,
        # ?active=false → archived only. Models without an `active` field
        # ignore this entirely.
        if 'active' in spec['fields']:
            flag = str(params.get('active', '')).lower()
            if flag == 'all':
                domain = ['|', ('active', '=', True), ('active', '=', False)]
            elif flag in ('0', 'false'):
                domain = [('active', '=', False)]
            else:
                domain = [('active', '=', True)]
        records = request.env[spec['model']].sudo().search(
            domain, order=spec['order'])
        return _ok({
            'count': len(records),
            'items': [_serialize(r, spec) for r in records],
        })

    def _read_one(self, spec, rid):
        rec = request.env[spec['model']].sudo().browse(rid)
        if not rec.exists():
            return return_Response(message='Record %s not found' % rid,
                                   status=404)
        return _ok({'record': _serialize(rec, spec)})

    def _create(self, spec):
        if not _is_admin():
            return return_Response(
                message='Admin privileges required to create content.',
                status=403)
        vals, errors = _build_vals(spec, _read_params(), partial=False)
        if errors:
            return return_Response(message=', '.join(errors), status=400,
                                   errors=errors)
        try:
            rec = request.env[spec['model']].sudo().create(vals)
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Create %s failed: %s', spec['model'], exc)
            return return_Response(message=str(exc), status=400)
        return _ok({'record': _serialize(rec, spec)})

    def _update(self, spec, rid):
        if not _is_admin():
            return return_Response(
                message='Admin privileges required to edit content.',
                status=403)
        rec = request.env[spec['model']].sudo().browse(rid)
        if not rec.exists():
            return return_Response(message='Record %s not found' % rid,
                                   status=404)
        vals, errors = _build_vals(spec, _read_params(), partial=True)
        if errors:
            return return_Response(message=', '.join(errors), status=400,
                                   errors=errors)
        if not vals:
            return return_Response(message='No writable fields supplied.',
                                   status=400)
        try:
            rec.write(vals)
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Update %s failed: %s', spec['model'], exc)
            return return_Response(message=str(exc), status=400)
        return _ok({'record': _serialize(rec, spec)})

    def _delete(self, spec, rid):
        if not _is_admin():
            return return_Response(
                message='Admin privileges required to delete content.',
                status=403)
        rec = request.env[spec['model']].sudo().browse(rid)
        if not rec.exists():
            return return_Response(message='Record %s not found' % rid,
                                   status=404)
        try:
            rec.unlink()
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Delete %s failed: %s', spec['model'], exc)
            return return_Response(message=str(exc), status=400)
        return _ok({'deleted': rid})


class WikiGrievanceMemberController(http.Controller):
    """Owner-scoped read/update/delete for the logged-in employee's own
    grievances. Create/list already live in ``wiki_controllers.py``. Edits
    and deletes are only allowed while the grievance is still a draft."""

    def _own_grievance(self, rid):
        employee = request.env.user.employee_id
        if not employee:
            return None, return_Response(message='Employee profile not found.',
                                         status=404)
        rec = request.env['wiki.grievance'].sudo().browse(rid)
        if not rec.exists() or rec.employee_id.id != employee.id:
            return None, return_Response(message='Grievance not found.',
                                         status=404)
        return rec, None

    @http.route('/api/v1/wiki/grievances/<int:rid>', type='http', auth='none',
                methods=['GET', 'PUT', 'DELETE'], csrf=False, cors='*')
    @validate_token
    def grievance_member(self, rid, **kwargs):
        rec, err = self._own_grievance(rid)
        if err:
            return err
        method = request.httprequest.method
        if method == 'GET':
            return _ok({'record': {
                'id': rec.id,
                'reference': rec.reference,
                'category': rec.category,
                'description': rec.description,
                'is_anonymous': rec.is_anonymous,
                'state': rec.state,
            }})
        if rec.state != 'draft':
            return return_Response(
                message='Only draft grievances can be %s.'
                        % ('edited' if method == 'PUT' else 'deleted'),
                status=400)
        if method == 'DELETE':
            rec.unlink()
            return _ok({'deleted': rid})
        # PUT — update editable fields on a draft.
        params = _read_params()
        vals = {}
        if 'category' in params:
            valid = [c[0] for c in request.env['wiki.grievance'].CATEGORIES]
            if params['category'] not in valid:
                return return_Response(message='Invalid category.', status=400)
            vals['category'] = params['category']
        if 'description' in params:
            desc = (params.get('description') or '').strip()
            if not desc:
                return return_Response(message="'description' cannot be empty.",
                                       status=400)
            vals['description'] = desc
        if 'is_anonymous' in params:
            vals['is_anonymous'] = str(params['is_anonymous']).strip().lower() \
                in _TRUTHY
        if params.get('state') and params['state'] != 'draft':
            vals['state'] = 'submitted'
        if not vals:
            return return_Response(message='No writable fields supplied.',
                                   status=400)
        rec.write(vals)
        return _ok({'record': {
            'id': rec.id, 'reference': rec.reference, 'state': rec.state}})


class WikiLeaveMemberController(http.Controller):
    """Cancel a leave request the employee already applied for."""

    @http.route('/api/v1/wiki/leave/<int:rid>/cancel', type='http',
                auth='none', methods=['POST'], csrf=False, cors='*')
    @validate_token
    def leave_cancel(self, rid, **kwargs):
        employee = request.env.user.employee_id
        if not employee:
            return return_Response(message='Employee profile not found.',
                                   status=404)
        leave = request.env['hr.leave'].sudo().browse(rid)
        if not leave.exists() or leave.employee_id.id != employee.id:
            return return_Response(message='Leave request not found.',
                                   status=404)
        if leave.state not in ('draft', 'confirm'):
            return return_Response(
                message='Only pending leave requests can be cancelled.',
                status=400)
        try:
            # Pending requests can be withdrawn outright.
            leave.action_refuse() if leave.state == 'confirm' else None
            leave.unlink()
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Leave cancel failed: %s', exc)
            return return_Response(message=str(exc), status=400)
        return _ok({'cancelled': rid})
