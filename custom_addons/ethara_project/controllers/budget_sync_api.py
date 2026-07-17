# -*- coding: utf-8 -*-
"""ethara_project AWS-pricing sync control HTTP API.

Exposes two operator endpoints that drive the AWS pricing catalog sync backed
by `ethara.project.aws.pricing.sync.log`:

- POST /api/v1/ethara_project/budget/infra/sync
      Triggers a full pricing sync via
      `aws_pricing_service.action_sync_all(...)`, commits, and returns the
      resulting sync-log row.
- GET  /api/v1/ethara_project/budget/infra/sync/status
      Returns the latest `ethara.project.aws.pricing.sync.log` row.

Both endpoints follow the module-wide conventions: `type='http'`,
`auth='none'`, `csrf=False`, `cors='*'`, and `@validate_token`. Responses use
the shared `return_Response` envelope with the serialized log under
`data={'data': {...}}`.
"""

import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.ethara_project.services import aws_pricing_service
from .budget_api import _coerce_int  # noqa: F401  (kept for module conventions)

_logger = logging.getLogger(__name__)

SYNC_LOG_MODEL = 'ethara.project.aws.pricing.sync.log'


def _serialize_sync_log(log):
    """Serialize an `ethara.project.aws.pricing.sync.log` record to a dict."""
    return {
        'batch_id': log.batch_id or '',
        'status': log.status or '',
        'services_fetched': log.services_fetched or 0,
        'services_upserted': log.services_upserted or 0,
        'items_fetched': log.items_fetched or 0,
        'items_upserted': log.items_upserted or 0,
        'api_call_count': log.api_call_count or 0,
        'started_at': log.started_at.isoformat() if log.started_at else None,
        'ended_at': log.ended_at.isoformat() if log.ended_at else None,
        'error_message': log.error_message or '',
    }


class EtharaBudgetSyncController(http.Controller):
    """Operator endpoints for triggering / inspecting AWS pricing syncs."""

    # -----------------------------------------------------------------------
    # POST /api/v1/ethara_project/budget/infra/sync
    #
    # Trigger a full AWS pricing sync. Delegates to
    # `aws_pricing_service.action_sync_all` (which always writes a sync-log
    # row, even when AWS credentials are missing and the run fails) and
    # returns the serialized log. The service commits its own work; we commit
    # again here so the log row is durably persisted before responding.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/sync',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    def trigger_infra_sync(self, **params):
        try:
            log = aws_pricing_service.action_sync_all(
                request.env,
                triggered_by='manual',
                user_id=request.env.uid,
            )
            request.env.cr.commit()
            return return_Response(
                message='AWS pricing sync completed.', status=200,
                data={'data': _serialize_sync_log(log)},
            )
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception('ethara_project budget/infra/sync failed')
            return return_Response(
                message='Failed to trigger AWS pricing sync.',
                status=400, errors=[str(e)],
            )

    # -----------------------------------------------------------------------
    # GET /api/v1/ethara_project/budget/infra/sync/status
    #
    # Return the most recent AWS pricing sync-log row (id desc, limit 1).
    # Returns data={'data': None} when no sync has ever run.
    # -----------------------------------------------------------------------
    @http.route(
        '/api/v1/ethara_project/budget/infra/sync/status',
        type='http', auth='none', methods=['GET'], csrf=False, cors='*',
    )
    @validate_token
    def infra_sync_status(self, **params):
        try:
            log = request.env[SYNC_LOG_MODEL].sudo().search(
                [], order='id desc', limit=1,
            )
            if not log:
                return return_Response(
                    message='No AWS pricing sync has run yet.', status=200,
                    data={'data': None},
                )
            return return_Response(
                message='Latest AWS pricing sync status fetched.', status=200,
                data={'data': _serialize_sync_log(log)},
            )
        except Exception as e:
            _logger.exception('ethara_project budget/infra/sync/status failed')
            return return_Response(
                message='Failed to fetch AWS pricing sync status.',
                status=400, errors=[str(e)],
            )
