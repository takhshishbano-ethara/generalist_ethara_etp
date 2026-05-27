"""HMAC-SHA256 verifier for the OpenRouter webhook."""
import hashlib
import hmac
import time

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "t2av")
class TestWebhookVerifier(TransactionCase):

    SECRET = "test_secret_abc"

    def _sign(self, body, ts=None, secret=None):
        ts = ts if ts is not None else int(time.time())
        secret = secret or self.SECRET
        payload = f"{ts}.".encode() + (body if isinstance(body, bytes) else body.encode())
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return f"t={ts},v1={sig}"

    def _verifier(self):
        return self.env["t2av.webhook.verifier"].sudo()

    def test_valid_signature_passes(self):
        body = b'{"hello":"world"}'
        header = self._sign(body)
        self.assertTrue(self._verifier().verify(body, header, self.SECRET))

    def test_wrong_secret_fails(self):
        body = b'{"hello":"world"}'
        header = self._sign(body, secret="wrong_secret")
        self.assertFalse(self._verifier().verify(body, header, self.SECRET))

    def test_stale_timestamp_fails(self):
        body = b'{"hello":"world"}'
        header = self._sign(body, ts=int(time.time()) - 1000)
        self.assertFalse(self._verifier().verify(body, header, self.SECRET, tolerance_seconds=300))

    def test_missing_parts_fail(self):
        body = b'{"hello":"world"}'
        self.assertFalse(self._verifier().verify(body, "v1=abc", self.SECRET))  # no ts
        self.assertFalse(self._verifier().verify(body, "t=123", self.SECRET))  # no sig
        self.assertFalse(self._verifier().verify(body, "", self.SECRET))  # empty
        self.assertFalse(self._verifier().verify(b"", "t=123,v1=abc", self.SECRET))  # empty body
        self.assertFalse(self._verifier().verify(body, "t=123,v1=abc", ""))  # empty secret
