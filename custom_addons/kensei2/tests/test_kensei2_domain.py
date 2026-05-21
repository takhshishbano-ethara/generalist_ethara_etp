# -*- coding: utf-8 -*-
from odoo.tests import tagged

from .common import Kensei2TestCase


@tagged("post_install", "-at_install")
class TestKensei2Domain(Kensei2TestCase):

    def test_create_domain(self):
        d = self.Domain.create({"name": "test-domain"})
        self.assertTrue(d.exists())
        self.assertEqual(d.name, "test-domain")

    def test_parent_child_relationship(self):
        parent = self.Domain.create({"name": "parent"})
        child1 = self.Domain.create({"name": "child1", "parent_id": parent.id})
        child2 = self.Domain.create({"name": "child2", "parent_id": parent.id})
        self.assertEqual(len(parent.child_ids), 2)
        self.assertIn(child1, parent.child_ids)
        self.assertIn(child2, parent.child_ids)
        self.assertEqual(child1.parent_id, parent)

    def test_rec_name_is_name(self):
        d = self.Domain.create({"name": "display-test"})
        self.assertEqual(d.display_name, "display-test")

    def test_md_file_fields_are_char(self):
        d = self.Domain.create({
            "name": "md-test",
            "md_file1": "/path/to/file1.md",
            "md_file2": "/path/to/file2.md",
            "md_file3": "/path/to/file3.md",
        })
        self.assertEqual(d.md_file1, "/path/to/file1.md")
        self.assertEqual(d.md_file2, "/path/to/file2.md")
        self.assertEqual(d.md_file3, "/path/to/file3.md")

    def test_create_without_name(self):
        d = self.Domain.create({})
        self.assertFalse(d.name)
