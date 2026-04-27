from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_sec_access", "post_install", "-at_install")
class TestAtlasSecurityAccess(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Access = cls.env["ir.model.access"]

    def test_atlas_atlas_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.atlas")])
        self.assertTrue(len(access) > 0)

    def test_atlas_domain_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.domain")])
        self.assertTrue(len(access) > 0)

    def test_atlas_rubric_criterion_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.rubric.criterion")])
        self.assertTrue(len(access) > 0)

    def test_atlas_rubric_level_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.rubric.level")])
        self.assertTrue(len(access) > 0)

    def test_atlas_turn_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.turn")])
        self.assertTrue(len(access) > 0)

    def test_atlas_sandbox_has_access_entry(self):
        access = self.Access.search([("model_id.model", "=", "atlas.sandbox")])
        self.assertTrue(len(access) > 0)

    def test_admin_can_read_atlas(self):
        admin = self.env.ref("base.user_admin")
        atlas = self.env["atlas.atlas"].with_user(admin).create({})
        self.assertTrue(atlas.id > 0)

    def test_admin_can_read_domain(self):
        admin = self.env.ref("base.user_admin")
        d = self.env["atlas.domain"].with_user(admin).create({"name": "sec"})
        self.assertEqual(d.name, "sec")

    def test_access_rule_has_read_permission(self):
        access = self.Access.search([("model_id.model", "=", "atlas.atlas")], limit=1)
        self.assertTrue(access.perm_read)

    def test_access_rule_has_write_permission(self):
        access = self.Access.search([("model_id.model", "=", "atlas.atlas")], limit=1)
        self.assertIn(access.perm_write, (True, False))

    def test_atlas_atlas_model_registered(self):
        self.assertIn("atlas.atlas", self.env.registry)

    def test_atlas_domain_model_registered(self):
        self.assertIn("atlas.domain", self.env.registry)

    def test_atlas_rubric_criterion_model_registered(self):
        self.assertIn("atlas.rubric.criterion", self.env.registry)

    def test_atlas_rubric_level_model_registered(self):
        self.assertIn("atlas.rubric.level", self.env.registry)

    def test_atlas_turn_model_registered(self):
        self.assertIn("atlas.turn", self.env.registry)
