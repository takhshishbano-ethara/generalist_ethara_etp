# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import tagged

from .common import TalosTestCase


@tagged("post_install", "-at_install")
class TestTalosSettings(TalosTestCase):

    def test_default_deployment_mode_is_local(self):
        settings = self.env["res.config.settings"].create({})
        self.assertIn(settings.talos_deployment_mode, ("local", False))

    def test_default_bedrock_region(self):
        settings = self.env["res.config.settings"].create({})
        region = settings.talos_bedrock_region or "ap-south-1"
        self.assertEqual(region, "ap-south-1")

    def test_docker_available_true_when_docker_returns_0(self):
        mock_result = patch("subprocess.run")
        with mock_result as mock_run:
            mock_run.return_value.returncode = 0
            settings = self.env["res.config.settings"].create({})
            settings._compute_talos_docker_available()
            self.assertTrue(settings.talos_docker_available)

    def test_docker_available_false_when_docker_returns_nonzero(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            settings = self.env["res.config.settings"].create({})
            settings._compute_talos_docker_available()
            self.assertFalse(settings.talos_docker_available)

    def test_docker_available_false_when_binary_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            settings = self.env["res.config.settings"].create({})
            settings._compute_talos_docker_available()
            self.assertFalse(settings.talos_docker_available)

    def test_docker_available_false_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)):
            settings = self.env["res.config.settings"].create({})
            settings._compute_talos_docker_available()
            self.assertFalse(settings.talos_docker_available)

    def test_config_params_persist(self):
        self._set_param("talos.bedrock_inference_arn", "arn:aws:bedrock:test:12345:inference-profile/test")
        self._set_param("talos.bedrock_region", "us-west-2")
        self._set_param("talos.deployment_mode", "k8s")

        self.assertEqual(
            self.ICP.get_param("talos.bedrock_inference_arn"),
            "arn:aws:bedrock:test:12345:inference-profile/test",
        )
        self.assertEqual(self.ICP.get_param("talos.bedrock_region"), "us-west-2")
        self.assertEqual(self.ICP.get_param("talos.deployment_mode"), "k8s")
