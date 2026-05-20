"""Crowley webhook controller — receives OpenRouter generation callbacks.

Inactive by default: returns 503 unless ``crowley.webhook_secret`` is set in
ir.config_parameter. When active, verifies the HMAC signature, enforces
idempotency (X-OpenRouter-Idempotency-Key + row lock on the attempt), and
dispatches to ``crowley.attempt._handle_webhook_event``.
"""
import json
import logging

from odoo import http
from odoo.http import request

from ..models import credential_manager

_logger = logging.getLogger(__name__)


class CrowleyWebhookController(http.Controller):

    @http.route(
        "/crowley/webhook",
        type="http", auth="public", csrf=False, methods=["POST"],
        save_session=False,
    )
    def openrouter_webhook(self, **kwargs):
        raw_body = request.httprequest.get_data(as_text=False) or b""
        sig_header = request.httprequest.headers.get("X-OpenRouter-Signature", "")
        idempotency_key = request.httprequest.headers.get(
            "X-OpenRouter-Idempotency-Key", "",
        ) or ""

        secret = credential_manager.get_webhook_secret(request.env)
        ICP = request.env["ir.config_parameter"].sudo()
        tolerance = int(ICP.get_param("crowley.webhook_signature_tolerance", "300"))

        if not secret:
            _logger.warning("Crowley webhook: no secret configured, rejecting")
            return request.make_json_response({"error": "not_configured"}, status=503)

        verifier = request.env["crowley.webhook.verifier"].sudo()
        if not verifier.verify(raw_body, sig_header, secret, tolerance):
            _logger.warning("Crowley webhook: invalid signature")
            return request.make_json_response({"error": "invalid_signature"}, status=401)

        try:
            payload = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except (ValueError, UnicodeDecodeError) as e:
            _logger.warning("Crowley webhook: invalid JSON: %s", e)
            return request.make_json_response({"error": "invalid_json"}, status=400)

        event_type = payload.get("type") or ""
        data = payload.get("data") or {}
        openrouter_job_id = data.get("id")
        status = data.get("status")
        if not openrouter_job_id or not status:
            return request.make_json_response({"error": "missing_fields"}, status=400)

        Attempt = request.env["crowley.attempt"].sudo()
        attempt = Attempt.search(
            [("openrouter_job_id", "=", openrouter_job_id)], limit=1,
        )
        if not attempt:
            _logger.warning(
                "Crowley webhook: unknown openrouter_job_id %s", openrouter_job_id,
            )
            return request.make_json_response({"status": "unknown_job"}, status=200)

        # Row-lock the attempt so concurrent webhook deliveries cannot both
        # pass the idempotency check and both fire downloads.
        request.env.cr.execute(
            "SELECT webhook_idempotency_key FROM crowley_attempt "
            "WHERE id = %s FOR UPDATE",
            (attempt.id,),
        )
        row = request.env.cr.fetchone()
        existing_key = row[0] if row else None
        if idempotency_key and existing_key == idempotency_key:
            _logger.info(
                "Crowley webhook: already processed (idempotency key %s)",
                idempotency_key,
            )
            return request.make_json_response(
                {"status": "already_processed"}, status=200,
            )

        attempt.write({"webhook_idempotency_key": idempotency_key or False})
        try:
            attempt._handle_webhook_event(event_type, data)
        except Exception:
            _logger.exception(
                "Crowley webhook: handler failed for attempt %s", attempt.id,
            )
            return request.make_json_response({"error": "handler_failed"}, status=500)

        return request.make_json_response({"status": "ok"}, status=200)
