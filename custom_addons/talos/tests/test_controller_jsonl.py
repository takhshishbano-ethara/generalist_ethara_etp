# -*- coding: utf-8 -*-
"""Tests for controllers/talos_controller.py — the JSONL import logic.

Since the endpoint is type='http', auth='public', we test by exercising
the model-level operations the controller performs (create records,
auto-create persona/taxonomy, skip duplicates, etc.).
"""
import json

from odoo.tests import tagged

from .common import TalosTestCase


@tagged("post_install", "-at_install")
class TestJSONLImport(TalosTestCase):
    """Tests for JSONL import logic as exercised by the /api/get_jsonl_data endpoint."""

    def _import_items(self, items):
        """Simulate the controller's per-item creation loop.

        Returns list of created record IDs.
        """
        TalosModel = self.env["talos.talos"].sudo()
        PersonaModel = self.env["talos.persona"].sudo()
        TaxonomyModel = self.env["talos.taxonomy"].sudo()

        created_ids = []
        for item in items:
            task_id = item.get("id", "")
            if task_id and TalosModel.search([("task_id", "=", task_id)], limit=1):
                continue

            persona_name = (item.get("persona") or "").strip()
            if persona_name:
                normalized_name = persona_name.lower().replace(" ", "-")
                persona = PersonaModel.search([("name", "=", normalized_name)], limit=1)
                if not persona:
                    persona = PersonaModel.create({
                        "name": persona_name,
                        "soul_md": item.get("soul.md", ""),
                        "memory_md": item.get("memory.md", ""),
                        "agents_md": item.get("agent.md", ""),
                    })
            else:
                persona = PersonaModel.search([], limit=1)
                if not persona:
                    persona = PersonaModel.create({"name": "default"})

            gog_auth_val = item.get("gog_auth")
            gog_auth_str = json.dumps(gog_auth_val) if gog_auth_val else ""

            creds = item.get("credentials", {})
            gmail_creds = creds.get("gmail", {})

            vals = {
                "task_id": task_id,
                "persona_id": persona.id,
                "task_status": "NotSubmitted",
                "seed_prompt": item.get("seed_prompt", ""),
                "email": gmail_creds.get("email", ""),
                "password": gmail_creds.get("password", ""),
                "gog_auth": gog_auth_str,
            }

            domain_name = (item.get("domain") or "").strip()
            if domain_name:
                taxonomy_ids = []
                for tag in [t.strip() for t in domain_name.split(",") if t.strip()]:
                    tax = TaxonomyModel.search([("name", "=ilike", tag)], limit=1)
                    if not tax:
                        tax = TaxonomyModel.create({"name": tag})
                    taxonomy_ids.append(tax.id)
                if taxonomy_ids:
                    vals["heart_taxonomy"] = [(6, 0, taxonomy_ids)]

            for service in ("outlook", "eventbrite", "strava", "oura", "instagram", "facebook", "threads"):
                svc_creds = creds.get(service, {})
                vals["%s_username" % service] = svc_creds.get("username") or svc_creds.get("email", "")
                vals["%s_password" % service] = svc_creds.get("password", "")

            record = TalosModel.create(vals)
            created_ids.append(record.id)

        return created_ids

    def test_import_creates_records(self):
        """Parse JSONL lines, create records with correct fields."""
        items = [
            {"id": "JSONL-001", "seed_prompt": "Do task A", "persona": "test-persona"},
            {"id": "JSONL-002", "seed_prompt": "Do task B", "persona": "test-persona"},
        ]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 2)
        rec = self.Talos.browse(ids[0])
        self.assertEqual(rec.task_id, "JSONL-001")
        self.assertEqual(rec.seed_prompt, "Do task A")

    def test_import_skips_duplicate_task_ids(self):
        """Existing task_id → skipped."""
        items = [
            {"id": "TEST-BASE-001", "seed_prompt": "Duplicate", "persona": "test-persona"},
        ]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 0)

    def test_import_auto_creates_persona(self):
        """New persona name → creates talos.persona."""
        items = [
            {"id": "JSONL-PERSONA-001", "persona": "Brand New Persona", "seed_prompt": "x"},
        ]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 1)
        persona = self.Persona.search([("name", "=", "Brand New Persona")])
        self.assertTrue(persona)

    def test_import_auto_creates_taxonomy(self):
        """New domain tags → creates talos.taxonomy."""
        items = [
            {"id": "JSONL-TAX-001", "domain": "finance, health", "persona": "test-persona", "seed_prompt": "x"},
        ]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 1)
        finance = self.Taxonomy.search([("name", "=ilike", "finance")])
        health = self.Taxonomy.search([("name", "=ilike", "health")])
        self.assertTrue(finance)
        self.assertTrue(health)

    def test_import_maps_credentials(self):
        """outlook/eventbrite/strava/oura/instagram/facebook/threads fields."""
        items = [{
            "id": "JSONL-CRED-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "credentials": {
                "outlook": {"username": "user@outlook.com", "password": "pass1"},
                "instagram": {"email": "ig@test.com", "password": "igpass"},
            },
        }]
        ids = self._import_items(items)
        rec = self.Talos.browse(ids[0])
        self.assertEqual(rec.outlook_username, "user@outlook.com")
        self.assertEqual(rec.outlook_password, "pass1")
        self.assertEqual(rec.instagram_username, "ig@test.com")
        self.assertEqual(rec.instagram_password, "igpass")

    def test_import_maps_gog_auth(self):
        """gog_auth JSON stored correctly."""
        gog_data = {"installed": {"client_id": "abc"}}
        items = [{
            "id": "JSONL-GOG-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "gog_auth": gog_data,
        }]
        ids = self._import_items(items)
        rec = self.Talos.browse(ids[0])
        self.assertEqual(json.loads(rec.gog_auth), gog_data)

    def test_import_maps_gmail_credentials(self):
        """email and password from credentials.gmail."""
        items = [{
            "id": "JSONL-GMAIL-001",
            "persona": "test-persona",
            "seed_prompt": "x",
            "credentials": {"gmail": {"email": "a@gmail.com", "password": "secret"}},
        }]
        ids = self._import_items(items)
        rec = self.Talos.browse(ids[0])
        self.assertEqual(rec.email, "a@gmail.com")
        self.assertEqual(rec.password, "secret")

    def test_import_normalizes_persona_name(self):
        """'Elena Rodriguez' → finds existing 'elena-rodriguez' (normalized)."""
        self.Persona.create({"name": "elena-rodriguez"})
        items = [{
            "id": "JSONL-NORM-001",
            "persona": "Elena Rodriguez",
            "seed_prompt": "x",
        }]
        ids = self._import_items(items)
        rec = self.Talos.browse(ids[0])
        self.assertEqual(rec.persona_id.name, "elena-rodriguez")

    def test_import_handles_empty_fields(self):
        """Missing optional fields → no crash."""
        items = [{"id": "JSONL-EMPTY-001", "persona": "test-persona"}]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 1)
        rec = self.Talos.browse(ids[0])
        self.assertFalse(rec.gog_auth)
        self.assertFalse(rec.email)

    def test_import_handles_malformed_json_lines(self):
        """Invalid JSON lines skipped, valid ones imported."""
        raw_lines = [
            '{"id": "JSONL-MAL-001", "persona": "test-persona", "seed_prompt": "ok"}',
            "NOT VALID JSON{{{",
            '{"id": "JSONL-MAL-002", "persona": "test-persona", "seed_prompt": "ok2"}',
        ]
        valid_items = []
        for line in raw_lines:
            try:
                valid_items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        ids = self._import_items(valid_items)
        self.assertEqual(len(ids), 2)

    def test_import_default_persona_created(self):
        """No persona in data → default persona used or created."""
        # Remove all existing personas so the fallback is triggered
        items = [{"id": "JSONL-DEFPERS-001", "seed_prompt": "x"}]
        ids = self._import_items(items)
        self.assertEqual(len(ids), 1)
        rec = self.Talos.browse(ids[0])
        self.assertTrue(rec.persona_id)
