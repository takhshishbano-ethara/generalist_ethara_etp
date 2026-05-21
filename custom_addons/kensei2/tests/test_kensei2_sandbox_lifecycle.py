# -*- coding: utf-8 -*-
from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import Kensei2TestCase


@tagged("post_install", "-at_install")
class TestSandboxStartLocal(Kensei2TestCase):

    def test_action_start_sets_starting_status(self):
        sandbox = self.claude_sandbox
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._SANDBOX_POOL") as mock_pool:
            sandbox.action_start_sandbox()
            self.assertEqual(sandbox.docker_status, "starting")
            self.assertTrue(sandbox.docker_gateway_token)

    def test_action_start_allocates_ports(self):
        sandbox = self.claude_sandbox
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._SANDBOX_POOL"):
            sandbox.action_start_sandbox()
            self.assertGreater(sandbox.docker_port, 0)
            self.assertGreater(sandbox.docker_litellm_port, 0)

    def test_action_start_generates_gateway_token(self):
        sandbox = self.claude_sandbox
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._SANDBOX_POOL"):
            sandbox.action_start_sandbox()
            self.assertTrue(sandbox.docker_gateway_token)
            self.assertEqual(len(sandbox.docker_gateway_token), 64)

    def test_action_start_already_running_raises(self):
        sandbox = self.claude_sandbox
        sandbox.write({"docker_status": "running"})
        with self.assertRaises(UserError):
            sandbox.action_start_sandbox()

    def test_action_start_already_starting_raises(self):
        sandbox = self.claude_sandbox
        sandbox.write({"docker_status": "starting"})
        with self.assertRaises(UserError):
            sandbox.action_start_sandbox()

    def test_action_start_docker_unavailable_raises(self):
        self._set_param("kensei2.deployment_mode", "local")
        sandbox = self.claude_sandbox
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=False):
            with self.assertRaises(UserError):
                sandbox.action_start_sandbox()

    def test_action_start_compose_missing_raises(self):
        self._set_param("kensei2.deployment_mode", "local")
        sandbox = self.claude_sandbox
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=None):
            with self.assertRaises(UserError):
                sandbox.action_start_sandbox()

    def test_action_start_no_persona_raises(self):
        task_no_persona = self.Talos.with_context(skip_ensure_sandboxes=True).create({
            "persona_id": self.persona.id,
            "task_status": "NotSubmitted",
        })
        task_no_persona.write({"persona_id": False})
        sandbox = self.Sandbox.search([("kensei2_id", "=", task_no_persona.id)], limit=1)
        if sandbox:
            self._set_param("kensei2.deployment_mode", "local")
            with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
                 patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]):
                with self.assertRaises(UserError):
                    sandbox.action_start_sandbox()

    def test_action_start_resets_auto_hint_state(self):
        sandbox = self.claude_sandbox
        sandbox.write({
            "auto_hint_status": "evaluating",
            "auto_hint_iteration": 3,
            "auto_hint_group_id": "old-group",
        })
        with patch("odoo.addons.kensei2.models.kensei2_sandbox._docker_available", return_value=True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox._SANDBOX_POOL"):
            sandbox.action_start_sandbox()
            self.assertEqual(sandbox.auto_hint_status, "idle")
            self.assertEqual(sandbox.auto_hint_iteration, 0)
            self.assertFalse(sandbox.auto_hint_group_id)


@tagged("post_install", "-at_install")
class TestSandboxStartK8s(Kensei2TestCase):

    def test_start_k8s_bg_calls_deploy(self):
        self._set_param("kensei2.deployment_mode", "k8s")
        sandbox = self.claude_sandbox
        sandbox.write({"docker_status": "starting", "docker_gateway_token": "test-token"})

        with patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.K8S_AVAILABLE", True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.config"), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.client") as mock_k8s_client:
            mock_core = MagicMock()
            mock_apps = MagicMock()
            mock_networking = MagicMock()
            mock_k8s_client.CoreV1Api.return_value = mock_core
            mock_k8s_client.AppsV1Api.return_value = mock_apps
            mock_k8s_client.NetworkingV1Api.return_value = mock_networking
            mock_apps.read_namespaced_deployment.side_effect = Exception("not found")

            with patch.object(type(sandbox.env["kensei2.sandbox.k8s"]), "get_sandbox_status", return_value="running"):
                sandbox._start_k8s_bg()
                self.assertEqual(sandbox.docker_status, "running")

    def test_start_k8s_bg_deploy_failure_sets_error(self):
        self._set_param("kensei2.deployment_mode", "k8s")
        sandbox = self.claude_sandbox
        sandbox.write({"docker_status": "starting", "docker_gateway_token": "test-token"})

        with patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.K8S_AVAILABLE", True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.config"), \
             patch.object(type(sandbox.env["kensei2.sandbox.k8s"]), "deploy_sandbox", side_effect=Exception("K8s API error")):
            sandbox._start_k8s_bg()
            self.assertEqual(sandbox.docker_status, "error")
            self.assertIn("K8s API error", sandbox.docker_error)


@tagged("post_install", "-at_install")
class TestSandboxStop(Kensei2TestCase):

    def test_stop_already_stopped_noop(self):
        sandbox = self.claude_sandbox
        self.assertEqual(sandbox.docker_status, "stopped")
        sandbox.action_stop_sandbox()
        self.assertEqual(sandbox.docker_status, "stopped")

    def test_stop_local_calls_compose_down(self):
        sandbox = self.claude_sandbox
        sandbox.write({
            "docker_status": "running",
            "docker_compose_project": "kensei2-test-claude",
            "docker_workdir": "/tmp/kensei2-test",
            "docker_port": 21001,
            "docker_litellm_port": 16001,
            "docker_gateway_token": "test-token",
        })
        self._set_param("kensei2.deployment_mode", "local")

        with patch("odoo.addons.kensei2.models.kensei2_sandbox._compose_cmd", return_value=["docker", "compose"]), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=True), \
             patch("shutil.rmtree"):
            mock_run.return_value.returncode = 0
            sandbox._stop_local()
            self.assertEqual(sandbox.docker_status, "stopped")
            self.assertFalse(sandbox.docker_compose_project)
            self.assertEqual(sandbox.docker_port, 0)

    def test_stop_k8s_calls_destroy(self):
        sandbox = self.claude_sandbox
        sandbox.write({
            "docker_status": "running",
            "docker_compose_project": "kensei2-sandbox-test",
            "docker_port": 18789,
            "docker_gateway_token": "test-token",
        })

        with patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.K8S_AVAILABLE", True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.config"), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.client") as mock_client:
            mock_core = MagicMock()
            mock_apps = MagicMock()
            mock_client.CoreV1Api.return_value = mock_core
            mock_client.AppsV1Api.return_value = mock_apps
            sandbox._stop_k8s()
            self.assertEqual(sandbox.docker_status, "stopped")


@tagged("post_install", "-at_install")
class TestSandboxReconcile(Kensei2TestCase):

    def test_cron_reconcile_noop_local_mode(self):
        self._set_param("kensei2.deployment_mode", "local")
        self.Sandbox._cron_reconcile()

    def test_cron_reconcile_updates_k8s_status(self):
        self._set_param("kensei2.deployment_mode", "k8s")
        sandbox = self.claude_sandbox
        sandbox.write({"docker_status": "starting"})

        with patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.K8S_AVAILABLE", True), \
             patch("odoo.addons.kensei2.models.kensei2_sandbox_k8s.config"), \
             patch.object(type(sandbox.env["kensei2.sandbox.k8s"]), "get_sandbox_status", return_value="running"):
            self.Sandbox._cron_reconcile()
            sandbox.invalidate_recordset()
            self.assertEqual(sandbox.docker_status, "running")
