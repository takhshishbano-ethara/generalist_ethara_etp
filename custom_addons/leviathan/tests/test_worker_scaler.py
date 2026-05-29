"""Tests for the v19.0.6.0.0 K8s auto-scaler.

Covers the ported-from-vegeta scaling math + hysteresis without ever
talking to a real Kubernetes API. ``leviathan.services.k8s_scaler`` is
patched at the call boundary so these tests run identically whether or
not the ``kubernetes`` python package is installed.

Three classes:

* ``TestWorkerScalerConfig`` — ICP defaults and int parsing.
* ``TestWorkerScalerLoad`` — the queue-state count the scaler reads.
* ``TestCronDispatchPrdJobs`` — guards (queue disabled / inprocess
  mode) plus the actual scale-up / scale-down / cooldown behaviour with
  the kubernetes client fully mocked.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests import tagged

from .common import LeviathanTestCase


SCALER_PATH = "odoo.addons.leviathan.services.k8s_scaler"


@tagged("post_install", "-at_install", "leviathan")
class TestWorkerScalerConfig(LeviathanTestCase):

    def test_defaults_match_vegeta_v19_0_2_6_0(self):
        from odoo.addons.leviathan.services.k8s_scaler import (
            worker_scaler_config,
        )
        # Clear any test-DB leftover values so we exercise the defaults
        for key in (
            "leviathan.worker_deployment_name",
            "leviathan.k8s_namespace",
            "leviathan.worker_min_replicas",
            "leviathan.worker_max_replicas",
            "leviathan.worker_target_concurrency",
            "leviathan.worker_scale_down_cooldown_s",
        ):
            self.ICP.set_param(key, "")
        cfg = worker_scaler_config(self.env)
        self.assertEqual(cfg["deployment_name"], "leviathan-prd-worker")
        self.assertEqual(cfg["namespace"], "leviathan")
        self.assertEqual(cfg["min_replicas"], 0)
        self.assertEqual(cfg["max_replicas"], 10)
        self.assertEqual(cfg["per_pod_concurrency"], 100)
        self.assertEqual(cfg["scale_down_cooldown_s"], 600)

    def test_overrides_parsed_as_int(self):
        from odoo.addons.leviathan.services.k8s_scaler import (
            worker_scaler_config,
        )
        self.ICP.set_param("leviathan.worker_min_replicas", "1")
        self.ICP.set_param("leviathan.worker_max_replicas", "3")
        self.ICP.set_param("leviathan.worker_target_concurrency", "20")
        cfg = worker_scaler_config(self.env)
        self.assertEqual(cfg["min_replicas"], 1)
        self.assertEqual(cfg["max_replicas"], 3)
        self.assertEqual(cfg["per_pod_concurrency"], 20)


@tagged("post_install", "-at_install", "leviathan")
class TestWorkerScalerLoad(LeviathanTestCase):

    def test_counts_only_pending_states(self):
        # Eligible
        self._create_job(state="generating")
        self._create_job(state="scoring")
        self._create_job(state="qc_running")
        # NOT eligible — extraction runs in Lambda, terminal states don't
        # cause scale-up.
        self._create_job(state="extracting")
        self._create_job(state="done", prd_text="x")
        self._create_job(state="failed")
        # Excluded by cancel_requested
        self._create_job(state="generating", cancel_requested=True)
        load = self.Job._worker_scaler_load()
        self.assertEqual(load, 3)


@tagged("post_install", "-at_install", "leviathan")
class TestCronDispatchPrdJobs(LeviathanTestCase):

    def _enable_worker_mode(self):
        self.ICP.set_param("leviathan.prd_queue_enabled", "True")
        self.ICP.set_param("leviathan.prd_execution_mode", "worker")

    def test_no_op_when_queue_disabled(self):
        self.ICP.set_param("leviathan.prd_queue_enabled", "False")
        with patch(f"{SCALER_PATH}.run_worker_deployment_scaler") as scaler:
            self.Job._cron_dispatch_prd_jobs()
        scaler.assert_not_called()

    def test_no_op_when_inprocess(self):
        self.ICP.set_param("leviathan.prd_queue_enabled", "True")
        self.ICP.set_param("leviathan.prd_execution_mode", "inprocess")
        with patch(f"{SCALER_PATH}.run_worker_deployment_scaler") as scaler:
            self.Job._cron_dispatch_prd_jobs()
        scaler.assert_not_called()

    def test_invokes_scaler_in_worker_mode(self):
        self._enable_worker_mode()
        self._create_job(state="generating")
        with patch(f"{SCALER_PATH}.run_worker_deployment_scaler") as scaler:
            self.Job._cron_dispatch_prd_jobs()
        scaler.assert_called_once()
        # Second arg is the load count
        self.assertEqual(scaler.call_args.args[1], 1)


@tagged("post_install", "-at_install", "leviathan")
class TestRunWorkerDeploymentScaler(LeviathanTestCase):
    """Exercises the scaler body itself with a mocked AppsV1Api."""

    def _mock_deployment(self, spec=0, status=0):
        dep = MagicMock()
        dep.spec.replicas = spec
        dep.status.replicas = status
        return dep

    def _set_defaults(self, min_r=0, max_r=10, per_pod=100, cooldown=600):
        self.ICP.set_param("leviathan.worker_min_replicas", str(min_r))
        self.ICP.set_param("leviathan.worker_max_replicas", str(max_r))
        self.ICP.set_param("leviathan.worker_target_concurrency", str(per_pod))
        self.ICP.set_param(
            "leviathan.worker_scale_down_cooldown_s", str(cooldown),
        )
        self.ICP.set_param("leviathan.worker_last_scale_change_utc", "")

    def _run(self, load, deployment):
        # We patch K8S_AVAILABLE → True and the API class so the scaler
        # body runs end-to-end without a real cluster.
        from odoo.addons.leviathan.services import k8s_scaler
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.return_value = deployment
        with patch.object(k8s_scaler, "K8S_AVAILABLE", True), \
             patch.object(k8s_scaler, "_load_k8s_config"), \
             patch.object(k8s_scaler, "k8s_client") as kc:
            kc.AppsV1Api.return_value = apps_v1
            k8s_scaler.run_worker_deployment_scaler(self.env, load)
        return apps_v1

    def test_scales_up_immediately(self):
        self._set_defaults(min_r=0, max_r=10, per_pod=100)
        apps_v1 = self._run(load=250, deployment=self._mock_deployment(spec=1, status=1))
        apps_v1.patch_namespaced_deployment.assert_called_once()
        body = apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
        # ceil(250/100) = 3
        self.assertEqual(body, {"spec": {"replicas": 3}})

    def test_no_op_when_at_desired(self):
        self._set_defaults(min_r=0, max_r=10, per_pod=100)
        apps_v1 = self._run(load=100, deployment=self._mock_deployment(spec=1, status=1))
        apps_v1.patch_namespaced_deployment.assert_not_called()

    def test_uses_max_of_spec_and_status_as_current(self):
        # spec=1 (already scaled down) but status=5 (4 pods still draining):
        # current must be 5. load=100 → desired=1. Hysteresis would block
        # the scale-down in real life, but with no last-scale recorded it
        # should patch through.
        self._set_defaults(min_r=0, max_r=10, per_pod=100, cooldown=600)
        apps_v1 = self._run(
            load=100,
            deployment=self._mock_deployment(spec=1, status=5),
        )
        apps_v1.patch_namespaced_deployment.assert_called_once()
        body = apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
        self.assertEqual(body, {"spec": {"replicas": 1}})

    def test_scale_down_cooldown_blocks_patch(self):
        self._set_defaults(min_r=0, max_r=10, per_pod=100, cooldown=600)
        # Pretend a scale-up happened 60s ago — cooldown of 600s means
        # any scale-down request right now must be deferred.
        recent = (fields.Datetime.now() - timedelta(seconds=60)).isoformat()
        self.ICP.set_param("leviathan.worker_last_scale_change_utc", recent)
        apps_v1 = self._run(
            load=0,
            deployment=self._mock_deployment(spec=3, status=3),
        )
        apps_v1.patch_namespaced_deployment.assert_not_called()

    def test_scale_down_after_cooldown_proceeds(self):
        self._set_defaults(min_r=0, max_r=10, per_pod=100, cooldown=60)
        old = (fields.Datetime.now() - timedelta(seconds=120)).isoformat()
        self.ICP.set_param("leviathan.worker_last_scale_change_utc", old)
        apps_v1 = self._run(
            load=0,
            deployment=self._mock_deployment(spec=3, status=3),
        )
        apps_v1.patch_namespaced_deployment.assert_called_once()
        body = apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
        self.assertEqual(body, {"spec": {"replicas": 0}})

    def test_clamps_to_max_replicas(self):
        self._set_defaults(min_r=0, max_r=5, per_pod=100)
        apps_v1 = self._run(
            load=99999,
            deployment=self._mock_deployment(spec=0, status=0),
        )
        body = apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
        self.assertEqual(body, {"spec": {"replicas": 5}})

    def test_clamps_to_min_replicas(self):
        self._set_defaults(min_r=2, max_r=10, per_pod=100)
        # No load + last-scale unset → cooldown gate doesn't engage.
        apps_v1 = self._run(
            load=0,
            deployment=self._mock_deployment(spec=0, status=0),
        )
        body = apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
        self.assertEqual(body, {"spec": {"replicas": 2}})

    def test_returns_early_when_k8s_unavailable(self):
        from odoo.addons.leviathan.services import k8s_scaler
        with patch.object(k8s_scaler, "K8S_AVAILABLE", False):
            # Should log a warning and return without raising
            k8s_scaler.run_worker_deployment_scaler(self.env, load=100)
