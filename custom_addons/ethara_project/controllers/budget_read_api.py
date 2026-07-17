# -*- coding: utf-8 -*-
"""ethara_project budget read/update HTTP API (Group A).

Ports `/budget/{list,detail,update,types}` from etp_projects onto
`ethara.project.budget`. Batch/phase replacement is intentionally NOT handled
here — phases are managed by the dedicated `/budget/batches/*` endpoints.
"""

import base64
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from .budget_api import (
    PROJECT_MODEL,
    BUDGET_MODEL,
    VALID_PRIORITIES,
    _coerce_int,
    _coerce_float,
    _pagination,
    _read_multipart_or_json,
    _missing_ids,
    _get_default_approver_user_ids,
    _parse_model_entry,
    _parse_infra_entry,
    _parse_subscription_entry,
)
from ._budget_serializers import budget_to_dict, budget_to_summary

_logger = logging.getLogger(__name__)


class EtharaBudgetReadController(http.Controller):

    # ------------------------------------------------------------------ list
    @http.route(
        '/api/v1/ethara_project/budget/list',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def list_budgets(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        Model = request.env[BUDGET_MODEL].sudo()
        domain = []
        project_id = _coerce_int(params.get('project_id'))
        if project_id:
            domain.append(('ethara_project_id', '=', project_id))
        budget_type = (params.get('budget_type') or '').strip()
        if budget_type:
            valid = [v for v, _ in Model._fields['project_type'].selection]
            if budget_type not in valid:
                return return_Response(
                    message=f'budget_type must be one of {valid}.',
                    status=400, data={},
                )
            domain.append(('project_type', '=', budget_type))
        state = (params.get('state') or '').strip()
        if state:
            valid = [v for v, _ in Model._fields['state'].selection]
            if state not in valid:
                return return_Response(
                    message=f'state must be one of {valid}.',
                    status=400, data={},
                )
            domain.append(('state', '=', state))
        search = (params.get('search') or '').strip()
        if search:
            domain += ['|', ('name', 'ilike', search),
                       ('description', 'ilike', search)]
        total = Model.search_count(domain)
        records = Model.search(
            domain, limit=limit, offset=offset, order='create_date desc, id desc',
        )
        return return_Response(
            message='Project budgets fetched.', status=200,
            data={
                'total': total, 'limit': limit, 'offset': offset,
                'budgets': [budget_to_summary(r) for r in records],
            },
        )

    # ---------------------------------------------------------------- detail
    @http.route(
        '/api/v1/ethara_project/budget/detail',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def budget_detail(self, **params):
        budget_id = _coerce_int(params.get('id'))
        if not budget_id:
            return return_Response(message='id is required.', status=400, data={})
        budget = request.env[BUDGET_MODEL].sudo().browse(budget_id).exists()
        if not budget:
            return return_Response(
                message='Project budget not found.', status=404, data={},
            )
        return return_Response(
            message='Project budget detail fetched.', status=200,
            data={'data': budget_to_dict(budget)},
        )

    # ----------------------------------------------------------------- types
    @http.route(
        '/api/v1/ethara_project/budget/types',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def budget_types(self, **params):
        Model = request.env[BUDGET_MODEL].sudo()

        def opts(field):
            return [
                {'value': v, 'label': lbl}
                for v, lbl in Model._fields[field].selection
            ]
        return return_Response(
            message='Budget field options fetched.', status=200,
            data={
                'budget_types': opts('project_type'),
                'states': opts('state'),
                'priorities': opts('priority'),
            },
        )

    # ---------------------------------------------------------------- update
    @http.route(
        '/api/v1/ethara_project/budget/update',
        type='http', auth='none', methods=['PATCH', 'POST'], csrf=False, cors='*',
    )
    @validate_token
    def update_budget(self, **params):
        jdata, uploaded_files = _read_multipart_or_json()
        budget_id = _coerce_int(jdata.get('id'))
        if not budget_id:
            return return_Response(message='id is required.', status=400, data={})
        Model = request.env[BUDGET_MODEL].sudo()
        budget = Model.browse(budget_id).exists()
        if not budget:
            return return_Response(
                message='Project budget not found.', status=404, data={},
            )

        vals = {}

        if 'project_id' in jdata:
            pid = _coerce_int(jdata.get('project_id'))
            if not pid or not request.env[PROJECT_MODEL].sudo().browse(pid).exists():
                return return_Response(
                    message='project_id must be a valid ethara project id.',
                    status=400, data={},
                )
            vals['ethara_project_id'] = pid

        if 'budget_type' in jdata:
            bt = (jdata.get('budget_type') or '').strip()
            valid = [v for v, _ in Model._fields['project_type'].selection]
            if bt not in valid:
                return return_Response(
                    message=f'budget_type must be one of {valid}.',
                    status=400, data={},
                )
            vals['project_type'] = bt

        if 'state' in jdata:
            st = (jdata.get('state') or '').strip()
            valid = [v for v, _ in Model._fields['state'].selection]
            if st not in valid:
                return return_Response(
                    message=f'state must be one of {valid}.', status=400, data={},
                )
            vals['state'] = st

        if 'name' in jdata:
            nm = (jdata.get('name') or '').strip()
            if not nm:
                return return_Response(
                    message='name cannot be empty.', status=400, data={},
                )
            vals['name'] = nm

        if 'description' in jdata:
            vals['description'] = (jdata.get('description') or '').strip() or False

        if 'buffer_pct' in jdata:
            vals['buffer_pct'] = _coerce_float(jdata.get('buffer_pct'), 0.0)

        if 'budget_amount' in jdata:
            vals['budget_amount'] = _coerce_float(jdata.get('budget_amount'), 0.0)

        if 'priority' in jdata:
            pr = (jdata.get('priority') or '').strip()
            if pr not in VALID_PRIORITIES:
                return return_Response(
                    message=f'priority must be one of {list(VALID_PRIORITIES)}.',
                    status=400, data={},
                )
            vals['priority'] = pr

        if 'total_no_of_tasks' in jdata or 'total_tasks' in jdata:
            tt = _coerce_int(
                jdata.get('total_no_of_tasks') or jdata.get('total_tasks'), 0,
            ) or 0
            if tt <= 0:
                return return_Response(
                    message='total_no_of_tasks must be > 0.', status=400, data={},
                )
            vals['total_tasks'] = tt

        if 'approver_ids' in jdata:
            raw = jdata.get('approver_ids') or []
            if not isinstance(raw, (list, tuple)):
                return return_Response(
                    message='approver_ids must be a list of user ids.',
                    status=400, data={},
                )
            payload_ids = [_coerce_int(x) for x in raw if _coerce_int(x)]
            default_ids = _get_default_approver_user_ids(request.env) or []
            approver_ids = list(dict.fromkeys(list(default_ids) + payload_ids))
            missing = _missing_ids('res.users', approver_ids)
            if missing:
                return return_Response(
                    message=f'Approver user ids do not exist: {missing}.',
                    status=400, data={},
                )
            vals['approver_user_ids'] = [(6, 0, approver_ids)]

        # Line rebuilds — full replace when the key is present.
        for key, parser, field in (
            ('models', _parse_model_entry, 'model_line_ids'),
            ('infra', _parse_infra_entry, 'infra_line_ids'),
            ('subscription', _parse_subscription_entry, 'subscription_line_ids'),
        ):
            if key not in jdata:
                continue
            raw = jdata.get(key) or []
            if not isinstance(raw, list):
                return return_Response(
                    message=f'{key} must be a list.', status=400, data={},
                )
            cmds = [(5, 0, 0)]
            for idx, entry in enumerate(raw):
                line_vals, _extra, err = parser(entry, idx)
                if err:
                    return return_Response(message=err, status=400, data={})
                cmds.append((0, 0, line_vals))
            vals[field] = cmds

        # Uploaded attachments -> ir.attachment + append to attachment_urls.
        if uploaded_files:
            existing = list(budget.attachment_urls and
                            budget.attachment_urls.split('\n') or [])
            for f in uploaded_files:
                data = f.read()
                if not data:
                    continue
                att = request.env['ir.attachment'].sudo().create({
                    'name': f.filename or 'attachment',
                    'datas': base64.b64encode(data),
                    'res_model': BUDGET_MODEL,
                    'res_id': budget.id,
                    'type': 'binary',
                })
                existing.append('/web/content/%s' % att.id)
            vals['attachment_urls'] = '\n'.join([u for u in existing if u])

        try:
            if vals:
                budget.write(vals)
        except (UserError, ValidationError) as e:
            request.env.cr.rollback()
            return return_Response(message=str(e), status=400, data={})
        except Exception:
            request.env.cr.rollback()
            _logger.exception('ethara_project budget/update failed')
            return return_Response(
                message='Failed to update project budget.', status=400, data={},
            )

        return return_Response(
            message='Project budget updated.', status=200,
            data={'data': budget_to_dict(budget)},
        )
