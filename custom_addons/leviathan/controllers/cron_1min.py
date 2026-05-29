"""HTTP cron endpoints for the 1-minute cadence: dispatch + reconcile.

Replaces the in-Odoo ``ir.cron`` driver for the PRD-queue dispatch and
reconcile loops. See ``_common.py`` for the auth + response helpers.

Endpoints
=========
POST /api/v1/leviathan/cron/dispatch    → scaler + drainer
POST /api/v1/leviathan/cron/reconcile   → drainer only (recovery branch)

Why POST (F-HIGH-3)
===================
These endpoints trigger DB writes (claim, recovery, scale patches).
GET is supposed to be safe + idempotent in HTTP semantics; using POST
prevents accidental triggers from monitoring probes, browser
bookmarks, ingress probes, and HTTP caches in front of the API.

Auth
====
Shared-secret header ``X-Leviathan-Token`` (see ``_common.check_token``).
Constant-time compared. Refuses traffic if no secret is configured.
"""
import logging

from odoo import http
from odoo.http import request, Response

from . import _common

_logger = logging.getLogger(__name__)


class LeviathanCron1Min(http.Controller):
    """1-minute cadence: PRD dispatch + reconcile."""

    @http.route(
        "/api/v1/leviathan/cron/dispatch",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def trigger_dispatch(self, **_kwargs) -> Response:
        if not _common.check_token():
            return _common.UNAUTHORIZED
        try:
            # In worker mode (production) the dispatch tick scales the
            # ``leviathan-prd-worker`` Deployment based on queue depth.
            # In inprocess mode (local dev) the scaler is a no-op for K8s,
            # so we also run the in-Odoo drainer here to keep local dev
            # usable without a worker pod. Both methods self-guard by
            # mode + role so the wrong branch is a cheap return.
            Job = request.env["leviathan.job"].sudo()
            Job._cron_dispatch_prd_jobs()
            Job._cron_prd_queue_drainer()
            return _common.ok({
                "status": "ok",
                "cron": "dispatch_prd_jobs+queue_drainer",
            })
        except Exception as exc:
            return _common.server_error(exc)

    @http.route(
        "/api/v1/leviathan/cron/reconcile",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def trigger_reconcile(self, **_kwargs) -> Response:
        # Same drainer entry as dispatch (``_cron_prd_queue_drainer``
        # owns recovery via ``_prd_queue_recover_stale`` inside the
        # advisory lock). The separate URL exists so ops can schedule
        # reconcile on a different cadence during incidents — e.g.
        # bump reconcile to every 30s while keeping dispatch at 1m.
        # NOTE: F-CRIT-3 — the stock ``cronjobs.yaml`` schedules
        # both at 1m, which is waste. Either delete the reconcile
        # CronJob or set a different schedule.
        if not _common.check_token():
            return _common.UNAUTHORIZED
        try:
            request.env["leviathan.job"].sudo()._cron_prd_queue_drainer()
            return _common.ok({
                "status": "ok",
                "cron": "prd_queue_drainer",
            })
        except Exception as exc:
            return _common.server_error(exc)
