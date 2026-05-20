"""Crowley webhook HMAC verifier (HMAC-SHA256 with timestamp tolerance).

Used by ``/crowley/webhook`` to verify that incoming OpenRouter callbacks
are genuine. Stays inert until the ``crowley.webhook_secret`` ir.config_parameter
is set.
"""
import hashlib
import hmac
import logging
import time

from odoo import api, models

_logger = logging.getLogger(__name__)


class CrowleyWebhookVerifier(models.AbstractModel):
    _name = "crowley.webhook.verifier"
    _description = "HMAC-SHA256 verifier for OpenRouter video webhooks"

    @api.model
    def verify(self, raw_body, sig_header, secret, tolerance_seconds=300):
        """Validate an ``X-OpenRouter-Signature: t=<ts>,v1=<hex>`` header.

        Signed payload format: ``f"{ts}.".encode() + raw_body``.
        Returns True iff signature is well-formed, fresh (within tolerance),
        and verifies with constant-time comparison.
        """
        if not raw_body or not sig_header or not secret:
            return False
        try:
            ts_val = None
            sig_val = None
            for part in sig_header.split(","):
                k, _, v = part.strip().partition("=")
                if k == "t":
                    ts_val = int(v)
                elif k == "v1":
                    sig_val = v
            if ts_val is None or sig_val is None:
                return False
        except (ValueError, AttributeError):
            return False

        if abs(time.time() - ts_val) > tolerance_seconds:
            _logger.warning(
                "Crowley webhook: timestamp out of tolerance (%s vs %s)",
                ts_val, time.time(),
            )
            return False

        body_bytes = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
        signed_payload = f"{ts_val}.".encode() + body_bytes
        expected = hmac.new(
            secret.encode(), signed_payload, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig_val)
