# -*- coding: utf-8 -*-
"""Tests for controllers/kensei_controller.py — the JSONL import logic.

Each test calls the actual controller method (method_get_jsonl_data) with
patched odoo.http.request and requests.get so we exercise the real code
path end-to-end while controlling I/O.
"""
import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import KenseiTestCase
from ..controllers.kensei_controller import Kensei as KenseiController


def _make_mock_response(jsonl_lines):
    """Build a fake requests.Response whose .text is newline-joined JSONL."""
    text = "\n".join(jsonl_lines)
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


@tagged("post_install", "-at_install")
class TestJSONLImport(KenseiTestCase):
    """Tests for JSONL import logic as exercised by the /api/get_jsonl_data endpoint."""

    def _call_controller(self, jsonl_lines, url="https://example.com/data.jsonl"):
        """Call the real controller with mocked HTTP request body and requests.get."""
        body = json.dumps({"url": url}).encode("utf-8")
        ctrl = KenseiController()
        mock_resp = _make_mock_response(jsonl_lines)
        with patch("odoo.http.request") as mock_req, \
             patch("odoo.addons.kensei.controllers.kensei_controller.requests.get", return_value=mock_resp) as mock_get:
            mock_req.env = self.env
            mock_req.httprequest = MagicMock()
            mock_req.httprequest.stream = BytesIO(body)
            mock_req.httprequest.stream.read = BytesIO(body).read
            result = ctrl.method_get_jsonl_data()
        return result

    def _parse_response(self, result):
        """Extract the JSON body from an http.Response-like object."""
        if hasattr(result, "data"):
            return json.loads(result.data)
        if hasattr(result, "response") and hasattr(result.response, "__iter__"):
            data = b"".join(result.response)
            return json.loads(data)
        return json.loads(str(result))

    def test_import_creates_records(self):
        """Parse JSONL lines, create records with correct fields."""
        lines = [
            json.dumps({"id": "JSONL-001", "seed_prompt": "Do task A", "persona": "test-persona"}),
            json.dumps({"id": "JSONL-002", "seed_prompt": "Do task B", "persona": "test-persona"}),
        ]
        self._call_controller(lines)
        rec1 = self.Talos.search([("task_id", "=", "JSONL-001")])
        rec2 = self.Talos.search([("task_id", "=", "JSONL-002")])
        self.assertTrue(rec1)
        self.assertTrue(rec2)
        self.assertEqual(rec1.seed_prompt, "Do task A")
        self.assertEqual(rec2.seed_prompt, "Do task B")

    def test_import_skips_duplicate_task_ids(self):
        """Existing task_id → skipped."""
        lines = [
            json.dumps({"id": "TEST-BASE-001", "seed_prompt": "Duplicate", "persona": "test-persona"}),
        ]
        result = self._call_controller(lines)
        body = self._parse_response(result)
        self.assertTrue(body.get("success"))
        self.assertIn("0 records created", body.get("message", ""))

    def test_import_auto_creates_persona(self):
        """New persona name → creates kensei.persona."""
        lines = [
            json.dumps({"id": "JSONL-PERSONA-001", "persona": "Brand New Persona", "seed_prompt": "x"}),
        ]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-PERSONA-001")])
        self.assertTrue(rec)
        persona = self.Persona.search([("name", "=", "Brand New Persona")])
        self.assertTrue(persona)

    def test_import_auto_creates_taxonomy(self):
        """New domain tags → creates kensei.taxonomy."""
        lines = [
            json.dumps({"id": "JSONL-TAX-001", "domain": "finance, health", "persona": "test-persona", "seed_prompt": "x"}),
        ]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-TAX-001")])
        self.assertTrue(rec)
        finance = self.Taxonomy.search([("name", "=ilike", "finance")])
        health = self.Taxonomy.search([("name", "=ilike", "health")])
        self.assertTrue(finance)
        self.assertTrue(health)

    def test_import_maps_credentials(self):
        """outlook/eventbrite/strava/oura/instagram/facebook/threads fields."""
        lines = [json.dumps({
            "id": "JSONL-CRED-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "credentials": {
                "outlook": {"username": "user@outlook.com", "password": "pass1"},
                "instagram": {"email": "ig@test.com", "password": "igpass"},
            },
        })]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-CRED-001")])
        self.assertTrue(rec)
        self.assertEqual(rec.outlook_username, "user@outlook.com")
        self.assertEqual(rec.outlook_password, "pass1")
        self.assertEqual(rec.instagram_username, "ig@test.com")
        self.assertEqual(rec.instagram_password, "igpass")

    def test_import_maps_gog_auth(self):
        """gog_auth JSON stored correctly."""
        gog_data = {"installed": {"client_id": "abc"}}
        lines = [json.dumps({
            "id": "JSONL-GOG-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "gog_auth": gog_data,
        })]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-GOG-001")])
        self.assertTrue(rec)
        self.assertEqual(json.loads(rec.gog_auth), gog_data)

    def test_import_maps_gmail_credentials(self):
        """email and password from credentials.gmail."""
        lines = [json.dumps({
            "id": "JSONL-GMAIL-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "credentials": {"gmail": {"email": "a@gmail.com", "password": "secret"}},
        })]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-GMAIL-001")])
        self.assertTrue(rec)
        self.assertEqual(rec.email, "a@gmail.com")
        self.assertEqual(rec.password, "secret")

    def test_import_normalizes_persona_name(self):
        """'Elena Rodriguez' → finds existing 'elena-rodriguez' (normalized)."""
        self.Persona.create({"name": "elena-rodriguez"})
        lines = [json.dumps({
            "id": "JSONL-NORM-001",
            "persona": "Elena Rodriguez",
            "seed_prompt": "x",
        })]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-NORM-001")])
        self.assertTrue(rec)
        self.assertEqual(rec.persona_id.name, "elena-rodriguez")

    def test_import_handles_empty_fields(self):
        """Missing optional fields → no crash."""
        lines = [json.dumps({"id": "JSONL-EMPTY-001", "persona": "test-persona"})]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-EMPTY-001")])
        self.assertTrue(rec)
        self.assertFalse(rec.gog_auth)
        self.assertFalse(rec.email)

    def test_import_handles_malformed_json_lines(self):
        """Invalid JSON lines skipped, valid ones imported."""
        lines = [
            '{"id": "JSONL-MAL-001", "persona": "test-persona", "seed_prompt": "ok"}',
            "NOT VALID JSON{{{",
            '{"id": "JSONL-MAL-002", "persona": "test-persona", "seed_prompt": "ok2"}',
        ]
        self._call_controller(lines)
        rec1 = self.Talos.search([("task_id", "=", "JSONL-MAL-001")])
        rec2 = self.Talos.search([("task_id", "=", "JSONL-MAL-002")])
        self.assertTrue(rec1)
        self.assertTrue(rec2)

    def test_import_default_persona_created(self):
        """No persona in data → default persona used or created."""
        lines = [json.dumps({"id": "JSONL-DEFPERS-001", "seed_prompt": "x"})]
        self._call_controller(lines)
        rec = self.Talos.search([("task_id", "=", "JSONL-DEFPERS-001")])
        self.assertTrue(rec)
        self.assertTrue(rec.persona_id)
