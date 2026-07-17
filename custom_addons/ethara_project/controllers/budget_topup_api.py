# -*- coding: utf-8 -*-
"""ethara_project budget top-up HTTP API.

Ports the etp_projects top-up flow onto the ethara data models:

    etp.project.budget.topup        -> ethara.project.budget.topup
    etp.batch.budget.topup.reason   -> ethara.project.phase.topup.reason
    topup.batch_id                  -> topup.phase_id
    topup.project_budget_id         -> topup.budget_id

Endpoints (all HTTP, auth='none', csrf disabled, token-validated):

    GET  /api/v1/ethara_project/budget/topup_reasons
    POST /api/v1/ethara_project/topup/create
    POST /api/v1/ethara_project/topup/approve
    POST /api/v1/ethara_project/topup/reject

Flutter sends the etp param names (`project_budget_id`, `topup_id`, ...); we
resolve those onto the renamed ethara fields but keep the JSON *response* field
names identical to the etp serializer so the existing Flutter models parse
unchanged.
"""

import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import UserError, ValidationError

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .budget_api import (
    BUDGET_MODEL,
    _coerce_int,
    _coerce_float,
    _pagination,
    _read_multipart_or_json,
)

_logger = logging.getLogger(__name__)

TOPUP_MODEL = 'ethara.project.budget.topup'
TOPUP_REASON_MODEL = 'ethara.project.phase.topup.reason'


def _topup_reason_brief(reason):
    """Catalog row for `budget/topup_reasons`.

    The ethara reason model has no `code` field; emit an empty string so the
    Flutter `budgetTopupReasons` const keeps the etp response shape.
    """
    if not reason:
        return None
    return {
        'id': reason.id,
        'name': reason.name or '',
        'code': '',
        'description': reason.description or '',
        'sequence': reason.sequence or 0,
        'active': bool(reason.active),
    }


def _topup_to_dict(topup):
    """Serialize a top-up, keeping the etp JSON field names.

    `project_budget_id`/`project_budget_name`/`project_id`/`project_name` are
    kept as response keys even though the ethara field is `budget_id` and the
    project FK is `ethara_project_id`.
    """
    topup.ensure_one()
    budget = topup.budget_id
    project = topup.ethara_project_id
    return {
        'id': topup.id,
        'name': topup.name or '',
        'project_budget_id': budget.id if budget else False,
        'project_budget_name': budget.name if budget else '',
        'project_id': project.id if project else False,
        'project_name': project.name if project else '',
        'amount': topup.amount or 0.0,
        'reason_id': topup.reason_id.id if topup.reason_id else False,
        'reason_name': topup.reason_id.name if topup.reason_id else '',
        'justification': topup.justification or '',
        'state': topup.state or 'draft',
        'requester_id': topup.requester_id.id if topup.requester_id else False,
        'requester_name': topup.requester_id.name if topup.requester_id else '',
        'approver_id': topup.approver_id.id if topup.approver_id else False,
        'approver_name': topup.approver_id.name if topup.approver_id else '',
        'approval_date': (
            topup.approval_date.strftime('%Y-%m-%d %H:%M:%S')
            if topup.approval_date else ''
        ),
        'rejection_reason': topup.rejection_reason or '',
    }


class EtharaBudgetTopupController(http.Controller):

    # ------------------------------------------------------- topup reasons
    @http.route(
        '/api/v1/ethara_project/budget/topup_reasons',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def list_topup_reasons(self, **params):
        limit, offset, err = _pagination(params)
        if err:
            return err
        domain = []
        if not _coerce_int(params.get('include_inactive'), 0):
            domain.append(('active', '=', True))
        search = (params.get('search') or '').strip()
        if search:
            # ethara reason model has no `code`; search on `name` only.
            domain.append(('name', 'ilike', search))
        Reason = request.env[TOPUP_REASON_MODEL].sudo()
        total = Reason.search_count(domain)
        records = Reason.search(
            domain, limit=limit, offset=offset, order='sequence, name',
        )
        items = [_topup_reason_brief(r) for r in records]
        return return_Response(
            message='Top-up reasons fetched.', status=200,
            data={
                'total': total,
                'limit': limit,
                'offset': offset,
                'topup_reasons': items,
            },
        )

    # -------------------------------------------------------------- create
    @http.route(
        '/api/v1/ethara_project/topup/create',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def create_topup(self, **params):
        try:
            jdata, _files = _read_multipart_or_json()

            budget_id = _coerce_int(
                jdata.get('project_budget_id') or jdata.get('budget_id')
            )
            if not budget_id:
                return return_Response(
                    message="'project_budget_id' (int) is required.",
                    status=400, data={},
                )
            budget = request.env[BUDGET_MODEL].sudo().browse(budget_id).exists()
            if not budget:
                return return_Response(
                    message='Project budget %s not found.' % budget_id,
                    status=404, data={},
                )

            amount = _coerce_float(jdata.get('amount'), None)
            if amount is None:
                return return_Response(
                    message="'amount' must be a positive number.",
                    status=400, data={},
                )
            if amount <= 0:
                return return_Response(
                    message="'amount' must be greater than zero.",
                    status=400, data={},
                )

            justification = (jdata.get('justification') or '').strip()
            if not justification:
                return return_Response(
                    message="'justification' is required.",
                    status=400, data={},
                )

            vals = {
                'budget_id': budget_id,
                'amount': amount,
                'justification': justification,
            }

            reason_id = _coerce_int(jdata.get('reason_id'))
            if reason_id:
                reason = request.env[TOPUP_REASON_MODEL].sudo().browse(
                    reason_id).exists()
                if not reason:
                    return return_Response(
                        message='Top-up reason %s not found.' % reason_id,
                        status=404, data={},
                    )
                vals['reason_id'] = reason_id

            submit = jdata.get('submit')
            submit = True if submit is None else bool(submit)

            topup = request.env[TOPUP_MODEL].sudo().create(vals)

            if submit:
                try:
                    topup.sudo().action_submit()
                except (UserError, ValidationError) as e:
                    return return_Response(
                        message=str(e), status=400, errors=[str(e)], data={},
                    )

            return return_Response(
                message='OK', status=200,
                data={'data': _topup_to_dict(topup)},
            )
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception('ethara_project topup/create failed')
            return return_Response(
                message='Something went wrong.', status=400,
                errors=[str(e)], data={},
            )

    # ------------------------------------------------------------- approve
    @http.route(
        '/api/v1/ethara_project/topup/approve',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def approve_topup(self, **params):
        try:
            jdata, _files = _read_multipart_or_json()

            topup_id = _coerce_int(jdata.get('topup_id'))
            if not topup_id:
                return return_Response(
                    message="'topup_id' (int) is required.",
                    status=400, data={},
                )

            topup = request.env[TOPUP_MODEL].sudo().browse(topup_id).exists()
            if not topup:
                return return_Response(
                    message='Top-up %s not found.' % topup_id,
                    status=404, data={},
                )

            try:
                topup.with_user(request.env.user).action_approve()
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, errors=[str(e)], data={},
                )

            return return_Response(
                message='OK', status=200,
                data={'data': _topup_to_dict(topup)},
            )
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception('ethara_project topup/approve failed')
            return return_Response(
                message='Something went wrong.', status=400,
                errors=[str(e)], data={},
            )

    # -------------------------------------------------------------- reject
    @http.route(
        '/api/v1/ethara_project/topup/reject',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def reject_topup(self, **params):
        try:
            jdata, _files = _read_multipart_or_json()

            topup_id = _coerce_int(jdata.get('topup_id'))
            if not topup_id:
                return return_Response(
                    message="'topup_id' (int) is required.",
                    status=400, data={},
                )

            reason = (jdata.get('reason') or '').strip()
            if not reason:
                return return_Response(
                    message="'reason' is required.", status=400, data={},
                )

            topup = request.env[TOPUP_MODEL].sudo().browse(topup_id).exists()
            if not topup:
                return return_Response(
                    message='Top-up %s not found.' % topup_id,
                    status=404, data={},
                )

            try:
                topup.with_user(request.env.user)._do_reject(reason)
            except (UserError, ValidationError) as e:
                return return_Response(
                    message=str(e), status=400, errors=[str(e)], data={},
                )

            return return_Response(
                message='OK', status=200,
                data={'data': _topup_to_dict(topup)},
            )
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception('ethara_project topup/reject failed')
            return return_Response(
                message='Something went wrong.', status=400,
                errors=[str(e)], data={},
            )
