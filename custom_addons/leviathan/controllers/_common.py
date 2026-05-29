"""Shared utilities for the Leviathan HTTP cron controllers.

Both ``cron_1min.py`` and ``cron_5min.py`` used to ship duplicated copies
of token verification + the 401 response constant. F-LOW-1 / F-HIGH-2 of
``STAGED_REVIEW.md`` flagged the divergence risk: the moment one of the
copies gets tweaked for an incident and the other doesn't, you have an
auth inconsistency between identically-named cron paths. Consolidating
them here also gives us a single place to enforce constant-time compare
(``hmac.compare_digest``) and a single point to audit if the token
scheme ever changes (e.g. moving cron to its own token — F-MED-4).
"""
from __future__ import annotations

import hmac
import json
import logging
import os

from odoo.http import request, Response

_logger = logging.getLogger(__name__)


# Reusable 401 response. Reference equality is fine — callers MUST NOT
# mutate the Response (Werkzeug treats it as immutable once frozen).
UNAUTHORIZED = Response(
    json.dumps({"error": "unauthorized"}),
    status=401,
    headers={"Content-Type": "application/json"},
)


def check_token() -> bool:
    """Verify the caller knows the cron/webhook token in constant time.

    Resolution order: ``leviathan.webhook_token`` System Parameter,
    then ``LEVIATHAN_WEBHOOK_TOKEN`` env var. Missing → refuse all
    traffic (allow-when-unset would be a footgun, especially given
    the route uses ``auth='none'``).

    Comparison uses :func:`hmac.compare_digest` so a network attacker
    cannot infer the token byte-by-byte from response-time deltas.
    The intra-cluster threat model makes this low risk in normal
    operation, but the change is free and prevents a class of leak.
    """
    icp = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("leviathan.webhook_token", "")
    )
    secret = icp or os.environ.get("LEVIATHAN_WEBHOOK_TOKEN") or ""
    if not secret:
        return False
    provided = request.httprequest.headers.get("X-Leviathan-Token", "")
    return hmac.compare_digest(provided.encode(), secret.encode())


def ok(payload: dict) -> Response:
    """200 JSON response. Centralised so the body shape is consistent."""
    return Response(
        json.dumps(payload),
        status=200,
        headers={"Content-Type": "application/json"},
    )


def server_error(exc: Exception) -> Response:
    """500 JSON response with a SAFE body.

    F-MED-9: the previous code f-stringed ``exc`` directly into the
    JSON body, producing malformed JSON when ``str(exc)`` contained a
    quote or newline, AND leaking exception detail (boto3 errors carry
    full request signing info). We log the real error to stderr and
    return a generic body so callers see a non-200 but operators see
    the cause in logs.
    """
    _logger.exception("cron handler failed: %s", exc)
    return Response(
        json.dumps({"error": "internal"}),
        status=500,
        headers={"Content-Type": "application/json"},
    )
