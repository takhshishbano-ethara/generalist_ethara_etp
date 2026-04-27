from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_smoke", "post_install", "-at_install")
class TestAtlasSmoke(TransactionCase):
    def test_001_env_available(self):
        self.assertIn("atlas.atlas", self.env.registry)

    def test_002_domain_model_available(self):
        self.assertIn("atlas.domain", self.env.registry)

    def test_003_rubric_model_available(self):
        self.assertIn("atlas.rubric.criterion", self.env.registry)
