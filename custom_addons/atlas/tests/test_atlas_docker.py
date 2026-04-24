# -*- coding: utf-8 -*-
from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestAtlasSandboxLocal(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.Domain = cls.env["atlas.domain"]
        cls.domain = cls.Domain.create({"name": "test-domain"})
        cls.task = cls.Atlas.create(
            {
                "task_id": "TEST-001",
                "parsona": cls.domain.id,
                "task_status": "NotSubmitted",
                "docker_persona": "elena",
            }
        )

    def test_docker_fields_default(self):
        self.assertEqual(self.task.docker_status, "stopped")
        self.assertFalse(self.task.docker_compose_project)
        self.assertFalse(self.task.docker_gateway_token)
        self.assertEqual(self.task.docker_port, 0)
        self.assertEqual(self.task.docker_litellm_port, 0)

    def test_default_persona(self):
        task = self.Atlas.create({"task_id": "TEST-002", "task_status": "NotSubmitted"})
        self.assertEqual(task.docker_persona, "marcus")

    def test_persona_stored(self):
        self.assertEqual(self.task.docker_persona, "elena")

    def test_start_sandbox_local_raises_when_docker_unavailable(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "atlas.deployment_mode", "local"
        )
        with patch(
            "odoo.addons.atlas.models.atlas._docker_available", return_value=False
        ):
            with patch.object(type(self.task), "user_has_groups", return_value=True):
                with self.assertRaises(UserError):
                    self.task.action_start_sandbox()

    def test_start_sandbox_local_raises_without_sandbox_dir(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "atlas.deployment_mode", "local"
        )
        self.env["ir.config_parameter"].sudo().set_param("atlas.sandbox_dir", "")
        with patch(
            "odoo.addons.atlas.models.atlas._docker_available", return_value=True
        ):
            with patch(
                "odoo.addons.atlas.models.atlas._compose_cmd",
                return_value=["docker", "compose"],
            ):
                with patch.object(
                    type(self.task.env.user), "has_group", return_value=True
                ):
                    with self.assertRaises(UserError):
                        self.task.action_start_sandbox()

    def test_stop_sandbox_when_already_stopped(self):
        self.task.action_stop_sandbox()
        self.assertEqual(self.task.docker_status, "stopped")

    def test_port_allocation(self):
        gateway, litellm, db = self.task._allocate_ports()
        offset = self.task.id % 1000
        self.assertEqual(gateway, 19000 + offset)
        self.assertEqual(litellm, 14000 + offset)
        self.assertEqual(db, 15432 + offset)

    def test_dashboard_url_local_running(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "atlas.deployment_mode", "local"
        )
        self.task.write(
            {
                "docker_status": "running",
                "docker_port": 19042,
                "docker_gateway_token": "abc123",
            }
        )
        self.assertEqual(
            self.task.docker_dashboard_url,
            "http://localhost:19042/#token=abc123",
        )

    def test_dashboard_url_when_stopped(self):
        self.assertFalse(self.task.docker_dashboard_url)


@tagged("post_install", "-at_install")
class TestAtlasSandboxModeDispatch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.task = cls.Atlas.create(
            {"task_id": "DISPATCH-001", "task_status": "NotSubmitted"}
        )

    def test_default_mode_is_local(self):
        mode = self.task._deployment_mode()
        self.assertEqual(mode, "local")

    def test_k8s_mode_dispatches_to_k8s(self):
        self.env["ir.config_parameter"].sudo().set_param("atlas.deployment_mode", "k8s")
        with patch.object(type(self.task), "_start_k8s") as mock_k8s:
            self.task.action_start_sandbox()
            mock_k8s.assert_called_once()

    def test_local_mode_dispatches_to_local(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "atlas.deployment_mode", "local"
        )
        with patch.object(type(self.task), "_start_local") as mock_local:
            self.task.action_start_sandbox()
            mock_local.assert_called_once()

    def test_dashboard_url_k8s_mode(self):
        self.env["ir.config_parameter"].sudo().set_param("atlas.deployment_mode", "k8s")
        self.task.write(
            {
                "docker_status": "running",
                "docker_gateway_token": "k8s-token-abc",
            }
        )
        self.assertIn("svc.cluster.local", self.task.docker_dashboard_url)
        self.assertIn("k8s-token-abc", self.task.docker_dashboard_url)


@tagged("post_install", "-at_install")
class TestAtlasSandboxK8s(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.task = cls.Atlas.create(
            {
                "task_id": "K8S-001",
                "task_status": "NotSubmitted",
                "docker_persona": "priya",
            }
        )

    def test_k8s_start_raises_when_unavailable(self):
        with patch("odoo.addons.atlas.models.atlas_sandbox_k8s.K8S_AVAILABLE", False):
            with self.assertRaises(UserError):
                self.env["atlas.sandbox.k8s"].deploy_sandbox(self.task)

    def test_k8s_destroy_noop_when_unavailable(self):
        with patch("odoo.addons.atlas.models.atlas_sandbox_k8s.K8S_AVAILABLE", False):
            self.env["atlas.sandbox.k8s"].destroy_sandbox(self.task)

    def test_k8s_status_stopped_when_unavailable(self):
        with patch("odoo.addons.atlas.models.atlas_sandbox_k8s.K8S_AVAILABLE", False):
            status = self.env["atlas.sandbox.k8s"].get_sandbox_status(self.task)
            self.assertEqual(status, "stopped")

    def test_reconcile_skips_when_no_active_tasks(self):
        self.env["ir.config_parameter"].sudo().set_param("atlas.deployment_mode", "k8s")
        with patch("odoo.addons.atlas.models.atlas_sandbox_k8s.K8S_AVAILABLE", True):
            self.task._cron_reconcile_sandboxes()
