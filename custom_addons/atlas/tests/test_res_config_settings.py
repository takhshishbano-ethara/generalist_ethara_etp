from unittest.mock import patch, MagicMock
import subprocess
from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("atlas", "atlas_rcs", "post_install", "-at_install")
class TestAtlasResConfigSettings(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env["ir.config_parameter"].sudo()
        cls.Settings = cls.env["res.config.settings"]

    def test_set_and_get_bedrock_arn(self):
        self.ICP.set_param("atlas.bedrock_inference_arn", "arn:aws:bedrock:...")
        self.assertEqual(self.ICP.get_param("atlas.bedrock_inference_arn"), "arn:aws:bedrock:...")

    def test_set_empty_returns_false(self):
        self.ICP.set_param("atlas.bedrock_region", "")
        self.assertFalse(self.ICP.get_param("atlas.bedrock_region"))

    def test_unset_returns_default(self):
        p = self.env["ir.config_parameter"].sudo().search([("key", "=", "atlas.made_up_param")])
        for r in p:
            r.unlink()
        self.assertEqual(self.ICP.get_param("atlas.made_up_param", "DEFAULT"), "DEFAULT")

    def test_update_param_overwrites(self):
        self.ICP.set_param("atlas.ws_router_host", "first.example.com")
        self.ICP.set_param("atlas.ws_router_host", "second.example.com")
        self.assertEqual(self.ICP.get_param("atlas.ws_router_host"), "second.example.com")

    def test_long_arn_value_preserved(self):
        v = "arn:" + "a" * 2000
        self.ICP.set_param("atlas.bedrock_model_arn", v)
        self.assertEqual(self.ICP.get_param("atlas.bedrock_model_arn"), v)

    def test_unicode_value_preserved(self):
        v = "\u4e2d\u6587/path"
        self.ICP.set_param("atlas.sandbox_dir", v)
        self.assertEqual(self.ICP.get_param("atlas.sandbox_dir"), v)

    def test_injection_value_preserved_as_raw_string(self):
        v = "'; DROP TABLE ir_config_parameter; --"
        self.ICP.set_param("atlas.litellm_master_key", v)
        self.assertEqual(self.ICP.get_param("atlas.litellm_master_key"), v)

    def test_setting_read_after_save(self):
        s = self.Settings.create({"atlas_bedrock_inference_arn": "test-arn-123"})
        s.set_values()
        self.assertEqual(self.ICP.get_param("atlas.bedrock_inference_arn"), "test-arn-123")

    def test_setting_deployment_mode_local(self):
        s = self.Settings.create({"atlas_deployment_mode": "local"})
        s.set_values()
        self.assertEqual(self.ICP.get_param("atlas.deployment_mode"), "local")

    def test_setting_deployment_mode_k8s(self):
        s = self.Settings.create({"atlas_deployment_mode": "k8s"})
        s.set_values()
        self.assertEqual(self.ICP.get_param("atlas.deployment_mode"), "k8s")

    def test_bedrock_region_default_ap_south_1(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", "atlas.bedrock_region")]).unlink()
        s = self.Settings.create({})
        self.assertEqual(s.atlas_bedrock_region, "ap-south-1")

    def test_openclaw_image_default_ghcr(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", "atlas.openclaw_image")]).unlink()
        s = self.Settings.create({})
        self.assertEqual(s.atlas_openclaw_image, "ghcr.io/openclaw/openclaw:latest")

    def test_litellm_image_default_berriai(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", "atlas.litellm_image")]).unlink()
        s = self.Settings.create({})
        self.assertEqual(s.atlas_litellm_image, "ghcr.io/berriai/litellm:main-stable")

    def test_aws_region_default_ap_south_1(self):
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", "atlas.aws_region")]).unlink()
        s = self.Settings.create({})
        self.assertEqual(s.atlas_aws_region, "ap-south-1")

    @patch("odoo.addons.atlas.models.res_config_settings.subprocess.run")
    def test_docker_available_when_returncode_0(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        s = self.Settings.create({})
        self.assertTrue(s.atlas_docker_available)

    @patch("odoo.addons.atlas.models.res_config_settings.subprocess.run")
    def test_docker_unavailable_when_returncode_1(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        s = self.Settings.create({})
        self.assertFalse(s.atlas_docker_available)

    @patch("odoo.addons.atlas.models.res_config_settings.subprocess.run",
           side_effect=FileNotFoundError("docker not found"))
    def test_docker_file_not_found(self, _):
        s = self.Settings.create({})
        self.assertFalse(s.atlas_docker_available)

    @patch("odoo.addons.atlas.models.res_config_settings.subprocess.run",
           side_effect=subprocess.TimeoutExpired("docker", 10))
    def test_docker_timeout(self, _):
        s = self.Settings.create({})
        self.assertFalse(s.atlas_docker_available)

    def test_all_atlas_params_roundtrip(self):
        params = [
            "atlas.bedrock_inference_arn", "atlas.bedrock_region", "atlas.sandbox_dir",
            "atlas.openclaw_image", "atlas.litellm_image", "atlas.ws_router_host",
            "atlas.aws_bearer_token", "atlas.aws_region", "atlas.bedrock_model_arn",
            "atlas.litellm_master_key", "atlas.litellm_db_password",
            "atlas.gog_client_secret", "atlas.gog_keyring_password",
        ]
        for p in params:
            self.ICP.set_param(p, "value_of_" + p)
            self.assertEqual(self.ICP.get_param(p), "value_of_" + p)
