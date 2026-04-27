# -*- coding: utf-8 -*-
"""Tests for models/talos_sandbox_k8s.py — the K8s deployer AbstractModel."""
import json
from unittest.mock import patch, MagicMock, PropertyMock

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TalosTestCase

# Module path used for patching
_K8S_MOD = "odoo.addons.talos.models.talos_sandbox_k8s"


def _make_api_exception(status):
    """Create a mock ApiException with the given HTTP status code."""
    exc = Exception("ApiException(%d)" % status)
    exc.status = status
    return exc


# ═══════════════════════════════════════════════════════════════════════
# 1. Helper / pure functions
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestK8sHelperFunctions(TalosTestCase):
    """Tests for module-level helper functions."""

    def test_sandbox_labels(self):
        """_sandbox_labels returns correct label dict with task-id."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _sandbox_labels
        labels = _sandbox_labels(self.claude_sandbox)
        self.assertEqual(labels["platform"], "talos")
        self.assertEqual(labels["component"], "sandbox")
        self.assertEqual(labels["task-id"], str(self.claude_sandbox.id))
        self.assertIn("app.kubernetes.io/name", labels)
        self.assertEqual(labels["app.kubernetes.io/managed-by"], "talos-odoo")

    def test_resource_name(self):
        """_resource_name returns 'talos-sandbox-{id}'."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _resource_name
        name = _resource_name(self.claude_sandbox)
        self.assertEqual(name, "talos-sandbox-%s" % self.claude_sandbox.id)

    def test_s3_session_path(self):
        """_s3_session_path returns correct S3 URI format."""
        from odoo.addons.talos.models.talos_sandbox_k8s import (
            _s3_session_path, S3_BUCKET, S3_TALOS_PREFIX,
        )
        path = _s3_session_path(self.claude_sandbox)
        expected = "s3://%s/%s/tasks/%s/sessions/" % (
            S3_BUCKET, S3_TALOS_PREFIX, self.claude_sandbox.id,
        )
        self.assertEqual(path, expected)

    def test_build_prestop_script(self):
        """_build_prestop_script contains task_id and persona."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _build_prestop_script
        script = _build_prestop_script(42, "elena")
        self.assertIn("42", script)
        self.assertIn("elena", script)
        self.assertIn("s3 sync", script)

    def test_build_openclaw_config_structure(self):
        """Config dict has gateway, browser, models, agents keys."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _build_openclaw_config
        cfg = _build_openclaw_config("tok-abc", {})
        self.assertIn("gateway", cfg)
        self.assertIn("browser", cfg)
        self.assertIn("models", cfg)
        self.assertIn("agents", cfg)

    def test_build_openclaw_config_gateway_auth(self):
        """Gateway token is embedded in config."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _build_openclaw_config
        cfg = _build_openclaw_config("my-secret-token", {})
        self.assertEqual(cfg["gateway"]["auth"]["token"], "my-secret-token")

    def test_build_openclaw_config_model_defaults(self):
        """agents.defaults.model matches MODEL_DEFAULTS for model_type."""
        from odoo.addons.talos.models.talos_sandbox_k8s import _build_openclaw_config
        from odoo.addons.talos.models.talos_sandbox import MODEL_DEFAULTS

        for model_type in ("claude", "glm"):
            cfg = _build_openclaw_config("tok", {}, model_type=model_type)
            self.assertEqual(
                cfg["agents"]["defaults"]["model"],
                MODEL_DEFAULTS[model_type],
            )


# ═══════════════════════════════════════════════════════════════════════
# 2. deploy_sandbox
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestK8sDeploy(TalosTestCase):
    """Tests for TalosSandboxK8s.deploy_sandbox — all K8s calls mocked."""

    def _get_deployer(self):
        return self.env["talos.sandbox.k8s"]

    @patch(_K8S_MOD + ".K8S_AVAILABLE", False)
    def test_deploy_k8s_unavailable_raises(self):
        """K8S_AVAILABLE=False → UserError."""
        deployer = self._get_deployer()
        with self.assertRaises(UserError):
            deployer.deploy_sandbox(self.claude_sandbox)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_no_persona_raises(self, _dotenv, _client, _config):
        """Missing persona → UserError."""
        deployer = self._get_deployer()
        task_no_persona = self.Talos.create({
            "task_id": "K8S-NOPERSONA",
            "persona_id": self.persona.id,
            "task_status": "NotSubmitted",
        })
        sandbox = task_no_persona.claude_sandbox_id
        # Remove persona link
        task_no_persona.write({"persona_id": False})
        with self.assertRaises(UserError):
            deployer.deploy_sandbox(sandbox)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_creates_secret(self, _dotenv, mock_client, _config):
        """create_namespaced_secret is called."""
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_net = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.NetworkingV1Api.return_value = mock_net

        deployer = self._get_deployer()
        deployer.deploy_sandbox(self.claude_sandbox)
        mock_core.create_namespaced_secret.assert_called()
        for call in mock_core.create_namespaced_secret.call_args_list:
            self.assertEqual(call[1]["namespace"], "talos")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_creates_persona_configmap(self, _dotenv, mock_client, _config):
        """create_namespaced_config_map is called for persona."""
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_net = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.NetworkingV1Api.return_value = mock_net

        deployer = self._get_deployer()
        deployer.deploy_sandbox(self.claude_sandbox)
        mock_core.create_namespaced_config_map.assert_called()
        for call in mock_core.create_namespaced_config_map.call_args_list:
            self.assertEqual(call[1]["namespace"], "talos")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_creates_deployment(self, _dotenv, mock_client, _config):
        """create_namespaced_deployment is called."""
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_net = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.NetworkingV1Api.return_value = mock_net

        deployer = self._get_deployer()
        deployer.deploy_sandbox(self.claude_sandbox)
        mock_apps.create_namespaced_deployment.assert_called()
        for call in mock_apps.create_namespaced_deployment.call_args_list:
            self.assertEqual(call[1]["namespace"], "talos")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_creates_service(self, _dotenv, mock_client, _config):
        """create_namespaced_service is called."""
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_net = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.NetworkingV1Api.return_value = mock_net

        deployer = self._get_deployer()
        deployer.deploy_sandbox(self.claude_sandbox)
        mock_core.create_namespaced_service.assert_called()
        for call in mock_core.create_namespaced_service.call_args_list:
            self.assertEqual(call[1]["namespace"], "talos")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_409_conflict_ignored(self, _dotenv, mock_client, _config):
        """ApiException(status=409) on create → no error (resource exists)."""
        exc_409 = _make_api_exception(409)
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_net = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.NetworkingV1Api.return_value = mock_net

        # Patch ApiException reference used in except clauses
        mock_client.rest.ApiException = type(exc_409)
        with patch(_K8S_MOD + ".ApiException", type(exc_409)):
            mock_core.create_namespaced_secret.side_effect = exc_409
            mock_core.create_namespaced_config_map.side_effect = exc_409
            mock_core.create_namespaced_service.side_effect = exc_409
            mock_apps.create_namespaced_deployment.side_effect = exc_409
            # WS router already exists
            mock_apps.read_namespaced_deployment.return_value = MagicMock()

            deployer = self._get_deployer()
            # Should not raise
            deployer.deploy_sandbox(self.claude_sandbox)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + "._load_dotenv", return_value={})
    def test_deploy_other_api_error_propagates(self, _dotenv, mock_client, _config):
        """ApiException(status=500) → raised."""
        exc_500 = _make_api_exception(500)
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps

        with patch(_K8S_MOD + ".ApiException", type(exc_500)):
            mock_core.create_namespaced_secret.side_effect = exc_500
            deployer = self._get_deployer()
            with self.assertRaises(Exception):
                deployer.deploy_sandbox(self.claude_sandbox)


# ═══════════════════════════════════════════════════════════════════════
# 3. destroy_sandbox
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestK8sDestroy(TalosTestCase):
    """Tests for TalosSandboxK8s.destroy_sandbox."""

    def _get_deployer(self):
        return self.env["talos.sandbox.k8s"]

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_destroy_deletes_resources(self, mock_client, _config):
        """delete calls for deployment, service, secrets, configmaps."""
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps

        deployer = self._get_deployer()
        deployer.destroy_sandbox(self.claude_sandbox)

        self.assertTrue(mock_apps.delete_namespaced_deployment.called)
        self.assertTrue(mock_core.delete_namespaced_service.called)
        self.assertTrue(mock_core.delete_namespaced_secret.called)
        self.assertTrue(mock_core.delete_namespaced_config_map.called)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", False)
    def test_destroy_k8s_unavailable_noop(self):
        """K8S_AVAILABLE=False → silent return (no error)."""
        deployer = self._get_deployer()
        # Should not raise
        deployer.destroy_sandbox(self.claude_sandbox)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_destroy_404_ignored(self, mock_client, _config):
        """ApiException(status=404) on delete → no error."""
        exc_404 = _make_api_exception(404)
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps

        with patch(_K8S_MOD + ".ApiException", type(exc_404)):
            mock_apps.delete_namespaced_deployment.side_effect = exc_404
            mock_core.delete_namespaced_service.side_effect = exc_404
            mock_core.delete_namespaced_secret.side_effect = exc_404
            mock_core.delete_namespaced_config_map.side_effect = exc_404

            deployer = self._get_deployer()
            # Should not raise
            deployer.destroy_sandbox(self.claude_sandbox)

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_destroy_other_error_logged(self, mock_client, _config):
        """non-404 ApiException → warning logged (not raised)."""
        exc_500 = _make_api_exception(500)
        mock_core = MagicMock()
        mock_apps = MagicMock()
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.AppsV1Api.return_value = mock_apps

        with patch(_K8S_MOD + ".ApiException", type(exc_500)):
            mock_apps.delete_namespaced_deployment.side_effect = exc_500
            mock_core.delete_namespaced_service.side_effect = exc_500
            mock_core.delete_namespaced_secret.side_effect = exc_500
            mock_core.delete_namespaced_config_map.side_effect = exc_500

            deployer = self._get_deployer()
            # _delete_resource catches non-404 and logs warning
            deployer.destroy_sandbox(self.claude_sandbox)


# ═══════════════════════════════════════════════════════════════════════
# 4. get_sandbox_status
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestK8sStatus(TalosTestCase):
    """Tests for TalosSandboxK8s.get_sandbox_status."""

    def _get_deployer(self):
        return self.env["talos.sandbox.k8s"]

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_status_running(self, mock_client, _config):
        """available_replicas ≥ 1 → 'running'."""
        mock_apps = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        dep_status = MagicMock()
        dep_status.available_replicas = 1
        dep_status.replicas = 1
        dep_mock = MagicMock()
        dep_mock.status = dep_status
        mock_apps.read_namespaced_deployment.return_value = dep_mock

        deployer = self._get_deployer()
        status = deployer.get_sandbox_status(self.claude_sandbox)
        self.assertEqual(status, "running")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_status_starting(self, mock_client, _config):
        """replicas > 0 but not available → 'starting'."""
        mock_apps = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        dep_status = MagicMock()
        dep_status.available_replicas = 0
        dep_status.replicas = 1
        dep_mock = MagicMock()
        dep_mock.status = dep_status
        mock_apps.read_namespaced_deployment.return_value = dep_mock

        deployer = self._get_deployer()
        status = deployer.get_sandbox_status(self.claude_sandbox)
        self.assertEqual(status, "starting")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    def test_status_stopped_404(self, mock_client, _config):
        """Deployment not found → 'stopped'."""
        exc_404 = _make_api_exception(404)
        mock_apps = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        with patch(_K8S_MOD + ".ApiException", type(exc_404)):
            mock_apps.read_namespaced_deployment.side_effect = exc_404
            deployer = self._get_deployer()
            status = deployer.get_sandbox_status(self.claude_sandbox)
            self.assertEqual(status, "stopped")

    @patch(_K8S_MOD + ".K8S_AVAILABLE", True)
    @patch(_K8S_MOD + ".config")
    @patch(_K8S_MOD + ".client")
    @patch(_K8S_MOD + ".fields")
    def test_status_error_timeout(self, mock_fields, mock_client, _config):
        """'starting' for >300s after 404 → 'error'."""
        from datetime import datetime, timedelta

        exc_404 = _make_api_exception(404)
        mock_apps = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps

        now = datetime.now()
        mock_fields.Datetime.now.return_value = now

        # Set sandbox to "starting" and write_date far in the past
        self.claude_sandbox.write({
            "docker_status": "starting",
        })
        # Monkey-patch write_date to simulate old timestamp
        old_time = now - timedelta(seconds=400)

        with patch(_K8S_MOD + ".ApiException", type(exc_404)):
            mock_apps.read_namespaced_deployment.side_effect = exc_404

            with patch.object(
                type(self.claude_sandbox), "write_date",
                new_callable=PropertyMock, return_value=old_time,
            ):
                deployer = self._get_deployer()
                status = deployer.get_sandbox_status(self.claude_sandbox)
                self.assertEqual(status, "error")
