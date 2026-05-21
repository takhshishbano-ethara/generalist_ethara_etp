# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import time

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "crowley_ai_vid_gen")
class TestWebhookVerifier(TransactionCase):

    def setUp(self):
        super().setUp()
        self.verifier = self.env["crowley.ai.vid.gen.webhook.verifier"]
        self.secret = "shared-secret-test"

    def _sign(self, body, ts=None, secret=None):
        secret = secret or self.secret
        ts = ts if ts is not None else int(time.time())
        if isinstance(body, str):
            body = body.encode("utf-8")
        signed = f"{ts}.".encode() + body
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={sig}"

    def test_valid_signature(self):
        body = b'{"type":"video.generation.completed","data":{}}'
        header = self._sign(body)
        self.assertTrue(self.verifier.verify(body, header, self.secret, 300))

    def test_wrong_secret_rejected(self):
        body = b'{"x":1}'
        header = self._sign(body)
        self.assertFalse(self.verifier.verify(body, header, "different-secret", 300))

    def test_tampered_body_rejected(self):
        body_original = b'{"x":1}'
        body_tampered = b'{"x":2}'
        header = self._sign(body_original)
        self.assertFalse(self.verifier.verify(body_tampered, header, self.secret, 300))

    def test_stale_timestamp_rejected(self):
        body = b'{"x":1}'
        old_ts = int(time.time()) - 1000  # way outside default tolerance
        header = self._sign(body, ts=old_ts)
        self.assertFalse(self.verifier.verify(body, header, self.secret, 300))

    def test_malformed_header_rejected(self):
        self.assertFalse(self.verifier.verify(b"{}", "garbage", self.secret, 300))
        self.assertFalse(self.verifier.verify(b"{}", "", self.secret, 300))
        self.assertFalse(self.verifier.verify(b"{}", "t=abc,v1=def", self.secret, 300))

    def test_empty_inputs_rejected(self):
        self.assertFalse(self.verifier.verify(b"", "anything", self.secret, 300))
        self.assertFalse(self.verifier.verify(b"x", "anything", "", 300))
