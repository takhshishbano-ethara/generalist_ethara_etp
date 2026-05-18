# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import SkollTestCase


@tagged("post_install", "-at_install")
class TestSkollPersonaCreate(SkollTestCase):

    def test_name_sanitized_to_lowercase_hyphen(self):
        p = self.Persona.create({"name": "Elena Rodriguez"})
        self.assertEqual(p.name, "elena-rodriguez")

    def test_name_stripped_whitespace(self):
        p = self.Persona.create({"name": "  padded  "})
        self.assertEqual(p.name, "padded")

    def test_write_sanitizes_name(self):
        p = self.Persona.create({"name": "original"})
        p.write({"name": "Updated Name"})
        self.assertEqual(p.name, "updated-name")

    def test_empty_name_raises_validation(self):
        with self.assertRaises(ValidationError):
            self.Persona.create({"name": ""})

    def test_whitespace_only_name_raises_validation(self):
        with self.assertRaises(ValidationError):
            self.Persona.create({"name": "   "})

    def test_duplicate_name_case_insensitive_raises(self):
        self.Persona.create({"name": "unique-persona"})
        with self.assertRaises(ValidationError):
            self.Persona.create({"name": "Unique Persona"})

    def test_duplicate_name_with_hyphens_raises(self):
        self.Persona.create({"name": "my-persona"})
        with self.assertRaises(ValidationError):
            self.Persona.create({"name": "my persona"})

    def test_active_defaults_true(self):
        p = self.Persona.create({"name": "active-test"})
        self.assertTrue(p.active)


@tagged("post_install", "-at_install")
class TestSkollPersonaComputed(SkollTestCase):

    def test_task_count_reflects_linked_tasks(self):
        p = self.Persona.create({"name": "count-persona"})
        self.assertEqual(p.task_count, 0)
        self._create_task(persona_id=p.id)
        p.invalidate_recordset()
        self.assertEqual(p.task_count, 1)
        self._create_task(persona_id=p.id)
        p.invalidate_recordset()
        self.assertEqual(p.task_count, 2)

    def test_ordering_by_name(self):
        self.Persona.create({"name": "zzz-last"})
        self.Persona.create({"name": "aaa-first"})
        all_names = self.Persona.search([]).mapped("name")
        self.assertEqual(all_names, sorted(all_names))

    def test_ondelete_restrict_blocks_deletion(self):
        p = self.Persona.create({"name": "restrict-persona"})
        self._create_task(persona_id=p.id)
        with self.assertRaises(Exception):
            p.unlink()

    def test_batch_create_sanitizes_all(self):
        records = self.Persona.create([
            {"name": "Batch One"},
            {"name": "Batch Two"},
        ])
        self.assertEqual(records[0].name, "batch-one")
        self.assertEqual(records[1].name, "batch-two")
