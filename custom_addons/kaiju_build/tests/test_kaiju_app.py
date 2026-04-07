# -*- coding: utf-8 -*-
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestKaijuApp(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.App = cls.env["kaiju.app"]
        cls.app_record = cls.App.create(
            {
                "name": "test-app",
                "repo_url": "test-org/test-repo",
                "default_branch": "main",
                "default_dockerfile": "Dockerfile",
            }
        )

    def test_create_app(self):
        self.assertEqual(self.app_record.name, "test-app")
        self.assertEqual(self.app_record.repo_url, "test-org/test-repo")
        self.assertEqual(self.app_record.default_branch, "main")

    def test_default_values(self):
        app = self.App.create({"name": "defaults-app"})
        self.assertEqual(app.default_branch, "main")
        self.assertEqual(app.default_dockerfile, "Dockerfile")
        self.assertTrue(app.active)

    def test_unique_name_constraint(self):
        with self.assertRaises(Exception):
            self.App.create({"name": "test-app"})

    def test_active_field_default(self):
        app = self.App.create({"name": "active-test"})
        self.assertTrue(app.active)

    def test_description_field(self):
        app = self.App.create(
            {
                "name": "desc-app",
                "description": "A test application",
            }
        )
        self.assertEqual(app.description, "A test application")
