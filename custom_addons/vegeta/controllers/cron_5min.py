import logging
import os

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

_UNAUTHORIZED = Response(
    '{"error": "unauthorized"}',
    status=401,
    headers={"Content-Type": "application/json"},
)


def _check_token() -> bool:
    icp = request.env["ir.config_parameter"].sudo().get_param("vegeta.webhook_token", "")
    secret = (
        icp
        or os.environ.get("VEGETA_WEBHOOK_TOKEN")
        or os.environ.get("LEVIATHAN_WEBHOOK_TOKEN")
    )
    if not secret:
        return False
    provided = request.httprequest.headers.get("X-Vegeta-Token", "")
    return provided == secret


class VegetaCron5Min(http.Controller):

    @http.route(
        "/api/v1/vegeta/cron/watchdog",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
    )
    def trigger_watchdog(self, **_kwargs):
        if not _check_token():
            return _UNAUTHORIZED
        try:
            request.env["vegeta.job"].sudo()._cron_watchdog_stuck_jobs()
            return Response(
                '{"status": "ok", "cron": "watchdog_stuck_jobs"}',
                status=200,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            _logger.exception("cron/watchdog failed")
            return Response(
                f'{{"error": "{exc}"}}',
                status=500,
                headers={"Content-Type": "application/json"},
            )
