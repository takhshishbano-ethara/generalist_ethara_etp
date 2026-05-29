"""HTTP cron endpoint for the 5-minute cadence: watchdog.

See ``cron_1min.py`` for the rationale around moving crons out of
Odoo's internal ``ir.cron`` and into HTTP endpoints driven by an
external scheduler (Kubernetes ``CronJob``).

POST so a monitoring probe / browser bookmark / cache cannot
accidentally fire stuck-job recovery (F-HIGH-3).

Endpoint
========
POST /api/v1/leviathan/cron/watchdog    → _cron_watchdog_stuck_jobs

Auth
====
Shared-secret header ``X-Leviathan-Token`` (see ``_common.check_token``).
"""
import logging

from odoo import http
from odoo.http import request, Response

from . import _common

_logger = logging.getLogger(__name__)


class LeviathanCron5Min(http.Controller):
    """5-minute cadence: extraction-side watchdog for stuck jobs."""

    @http.route(
        "/api/v1/leviathan/cron/watchdog",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def trigger_watchdog(self, **_kwargs) -> Response:
        if not _common.check_token():
            return _common.UNAUTHORIZED
        try:
            request.env["leviathan.job"].sudo()._cron_watchdog_stuck_jobs()
            return _common.ok({
                "status": "ok",
                "cron": "watchdog_stuck_jobs",
            })
        except Exception as exc:
            return _common.server_error(exc)
