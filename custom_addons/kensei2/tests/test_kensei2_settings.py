# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import tagged

from .common import Kensei2TestCase


@tagged("post_install", "-at_install")
class TestKensei2Settings(Kensei2TestCase):

    def test_default_deployment_mode_is_local(self):
        settings = self.env["res.config.settings"].create({})
        self.assertIn(settings.kensei2_deployment_mode, ("local", False))

    def test_default_bedrock_region(self):
        settings = self.env["res.config.settings"].create({})
        region = settings.kensei2_bedrock_region or "ap-south-1"
        self.assertEqual(region, "ap-south-1")

    def test_docker_available_true_when_docker_returns_0(self):
        mock_result = patch("subprocess.run")
        with mock_result as mock_run:
            mock_run.return_value.returncode = 0
            settings = self.env["res.config.settings"].create({})
            settings._compute_kensei2_docker_available()
            self.assertTrue(settings.kensei2_docker_available)

    def test_docker_available_false_when_docker_returns_nonzero(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            settings = self.env["res.config.settings"].create({})
            settings._compute_kensei2_docker_available()
            self.assertFalse(settings.kensei2_docker_available)

    def test_docker_available_false_when_binary_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            settings = self.env["res.config.settings"].create({})
            settings._compute_kensei2_docker_available()
            self.assertFalse(settings.kensei2_docker_available)

    def test_docker_available_false_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)):
            settings = self.env["res.config.settings"].create({})
            settings._compute_kensei2_docker_available()
            self.assertFalse(settings.kensei2_docker_available)

    def test_config_params_persist(self):
        self._set_param("kensei2.bedrock_inference_arn", "arn:aws:bedrock:test:12345:inference-profile/test")
        self._set_param("kensei2.bedrock_region", "us-west-2")
        self._set_param("kensei2.deployment_mode", "k8s")

        self.assertEqual(
            self.ICP.get_param("kensei2.bedrock_inference_arn"),
            "arn:aws:bedrock:test:12345:inference-profile/test",
        )
        self.assertEqual(self.ICP.get_param("kensei2.bedrock_region"), "us-west-2")
        self.assertEqual(self.ICP.get_param("kensei2.deployment_mode"), "k8s")
