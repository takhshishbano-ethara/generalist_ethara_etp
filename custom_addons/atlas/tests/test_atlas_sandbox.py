from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_sandbox", "post_install", "-at_install")
class TestAtlasSandbox(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.Sandbox = cls.env["atlas.sandbox"]

    def test_create_sandbox_with_atlas_id(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        self.assertEqual(s.atlas_id.id, t.id)

    def test_atlas_id_required(self):
        with self.assertRaises(Exception):
            self.Sandbox.create({"model_type": "glm"})

    def test_model_type_glm(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        self.assertEqual(s.model_type, "glm")

    def test_sandbox_unlink_removes(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        sid = s.id
        s.unlink()
        self.assertFalse(self.Sandbox.search([("id", "=", sid)]))

    def test_atlas_unlink_cascades_sandbox(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        sid = s.id
        t.unlink()
        self.assertFalse(self.Sandbox.search([("id", "=", sid)]))

    def test_docker_compose_project_name_pattern(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        if hasattr(s, "docker_compose_project") and s.docker_compose_project:
            self.assertIn(str(t.id), s.docker_compose_project)

    @patch("odoo.addons.atlas.models.atlas_sandbox.subprocess.run")
    def test_subprocess_mocked_during_tests(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"OK", stderr=b"")
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        self.assertTrue(s.id > 0)

    def test_multiple_sandboxes_per_atlas(self):
        t = self.Atlas.create({})
        s1 = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        s2 = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        self.assertNotEqual(s1.id, s2.id)
        self.assertEqual(len(t.sandbox_ids), 2)

    def test_sandbox_write_atlas_id_persists(self):
        t1 = self.Atlas.create({})
        t2 = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t1.id, "model_type": "glm"})
        s.atlas_id = t2
        self.assertEqual(s.atlas_id.id, t2.id)

    def test_sandbox_id_positive(self):
        t = self.Atlas.create({})
        s = self.Sandbox.create({"atlas_id": t.id, "model_type": "glm"})
        self.assertGreater(s.id, 0)
