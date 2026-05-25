"""Tests for the v19.0.2.5.0 worker-pool architecture.

Covers the four code paths introduced when PRD generation moved from
"one K8s Job per task" to "long-lived worker Deployment auto-scaled by a
cron":

1. ``_prd_execution_mode`` config parsing.
2. ``_worker_scaler_config`` config-param defaults and int parsing.
3. ``_run_worker_deployment_scaler`` scaling math + K8s patch behavior.
4. ``_run_reconcile_prd_jobs`` stale-heartbeat recovery.

Worker-side ``_claim_jobs`` is exercised against the live test cursor
because it is a thin SQL function and the daemon loop wrapping it is
trivial enough that mocking ``run_one`` covers it adequately.
"""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields

from .common import VegetaTestCase


class TestPrdExecutionMode(VegetaTestCase):
    def test_default_is_worker(self):
        self.ICP.set_param("vegeta.prd_execution_mode", "")
        self.assertEqual(self.Job._prd_execution_mode(), "worker")

    def test_inprocess_respected(self):
        self.ICP.set_param("vegeta.prd_execution_mode", "inprocess")
        self.assertEqual(self.Job._prd_execution_mode(), "inprocess")

    def test_invalid_falls_back_to_worker(self):
        self.ICP.set_param("vegeta.prd_execution_mode", "bogus_value")
        self.assertEqual(self.Job._prd_execution_mode(), "worker")

    def test_whitespace_and_case_normalised(self):
        self.ICP.set_param("vegeta.prd_execution_mode", "  WORKER  ")
        self.assertEqual(self.Job._prd_execution_mode(), "worker")


class TestWorkerScalerConfig(VegetaTestCase):
    def test_defaults(self):
        for key in (
            "vegeta.worker_deployment_name",
            "vegeta.k8s_namespace",
            "vegeta.worker_min_replicas",
            "vegeta.worker_max_replicas",
            "vegeta.worker_target_concurrency",
        ):
            self.ICP.set_param(key, "")
        cfg = self.Job._worker_scaler_config()
        self.assertEqual(cfg["deployment_name"], "vegeta-prd-worker")
        self.assertEqual(cfg["namespace"], "vegeta")
        self.assertEqual(cfg["min_replicas"], 1)
        self.assertEqual(cfg["max_replicas"], 10)
        self.assertEqual(cfg["per_pod_concurrency"], 100)

    def test_overrides(self):
        self.ICP.set_param("vegeta.worker_deployment_name", "custom-name")
        self.ICP.set_param("vegeta.k8s_namespace", "custom-ns")
        self.ICP.set_param("vegeta.worker_min_replicas", "2")
        self.ICP.set_param("vegeta.worker_max_replicas", "20")
        self.ICP.set_param("vegeta.worker_target_concurrency", "50")
        cfg = self.Job._worker_scaler_config()
        self.assertEqual(cfg["deployment_name"], "custom-name")
        self.assertEqual(cfg["namespace"], "custom-ns")
        self.assertEqual(cfg["min_replicas"], 2)
        self.assertEqual(cfg["max_replicas"], 20)
        self.assertEqual(cfg["per_pod_concurrency"], 50)


class TestWorkerDeploymentScaler(VegetaTestCase):
    """Exercises the scaling math without touching a real K8s cluster.

    Mocks ``K8S_AVAILABLE`` True, ``_load_k8s_config`` (no-op), and the
    ``AppsV1Api.read/patch_namespaced_deployment_scale`` boundary.
    """

    def _mock_k8s_deployment(self, spec_replicas, status_replicas=None):
        # Match the new read_namespaced_deployment shape: a V1Deployment-like
        # object with .spec.replicas and .status.replicas. Tests previously
        # mocked read_namespaced_deployment_scale + V1Scale; the scaler now
        # reads the full Deployment so it can compare spec vs status (H1 fix).
        if status_replicas is None:
            status_replicas = spec_replicas
        deployment = MagicMock()
        deployment.spec.replicas = spec_replicas
        deployment.status.replicas = status_replicas
        api = MagicMock()
        api.read_namespaced_deployment.return_value = deployment
        return api, deployment

    def _patches(self, current_replicas, status_replicas=None):
        api, deployment = self._mock_k8s_deployment(
            current_replicas, status_replicas,
        )
        # Reset the H2 hysteresis cooldown so scale-down tests aren't blocked
        # by a stale last-scale timestamp from a prior test.
        self.ICP.set_param("vegeta.worker_last_scale_change_utc", "")
        return patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ), api

    def _assert_patched_replicas(self, api, expected):
        api.patch_namespaced_deployment.assert_called_once()
        body = api.patch_namespaced_deployment.call_args.kwargs["body"]
        self.assertEqual(body["spec"]["replicas"], expected)

    def setUp(self):
        super().setUp()
        self.ICP.set_param("vegeta.worker_min_replicas", "1")
        self.ICP.set_param("vegeta.worker_max_replicas", "10")
        self.ICP.set_param("vegeta.worker_target_concurrency", "100")
        self.ICP.set_param("vegeta.worker_deployment_name", "vegeta-prd-worker")
        self.ICP.set_param("vegeta.k8s_namespace", "vegeta")

    def _make_jobs(self, n, state="generating", cancel=False):
        for _ in range(n):
            self._create_job(state=state, cancel_requested=cancel)

    def test_load_zero_clamps_to_min(self):
        # current=3, desired=1 (min) — scale-down path; hysteresis cooldown
        # is cleared by _patches so this fires immediately.
        patches, api = self._patches(current_replicas=3)
        with patches:
            self.Job._run_worker_deployment_scaler()
        self._assert_patched_replicas(api, 1)

    def test_load_under_one_pod_yields_one_replica(self):
        self._make_jobs(50)
        patches, api = self._patches(current_replicas=1)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # 50 jobs / 100 per pod = ceil(0.5) = 1, already at 1, no patch
        api.patch_namespaced_deployment.assert_not_called()

    def test_load_exactly_per_pod_yields_one_replica(self):
        self._make_jobs(100)
        patches, api = self._patches(current_replicas=1)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # 100 / 100 = 1 pod, already at 1
        api.patch_namespaced_deployment.assert_not_called()

    def test_load_overflow_scales_up(self):
        self._make_jobs(250)
        patches, api = self._patches(current_replicas=1)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # 250 / 100 = ceil(2.5) = 3 — scale UP always fires (no cooldown)
        self._assert_patched_replicas(api, 3)

    def test_load_above_max_clamps_to_max(self):
        self._make_jobs(5000)
        patches, api = self._patches(current_replicas=2)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # 5000 / 100 = 50, clamped to max=10
        self._assert_patched_replicas(api, 10)

    def test_cancelled_jobs_excluded_from_load(self):
        self._make_jobs(150, cancel=False)
        self._make_jobs(300, cancel=True)
        patches, api = self._patches(current_replicas=1)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # Only the 150 non-cancelled count → ceil(150/100) = 2
        self._assert_patched_replicas(api, 2)

    def test_already_at_desired_skips_patch(self):
        self._make_jobs(250)
        patches, api = self._patches(current_replicas=3)
        with patches:
            self.Job._run_worker_deployment_scaler()
        # 250 / 100 = 3, current is already 3 → no patch
        api.patch_namespaced_deployment.assert_not_called()

    def test_k8s_unavailable_returns_quietly(self):
        with patch(
            "odoo.addons.vegeta.models.vegeta_job.K8S_AVAILABLE", False,
        ):
            self.Job._run_worker_deployment_scaler()

    def test_k8s_api_failure_does_not_raise(self):
        api = MagicMock()
        api.read_namespaced_deployment.side_effect = Exception("boom")
        with patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ):
            self.Job._run_worker_deployment_scaler()

    def test_h2_scale_down_blocked_by_cooldown(self):
        # H2 regression test: scale-down within cooldown must NOT fire.
        # Set a recent timestamp to simulate "we just scaled".
        self.ICP.set_param(
            "vegeta.worker_last_scale_change_utc",
            fields.Datetime.now().isoformat(),
        )
        # current=3, load=0 → desired=1; without hysteresis would patch.
        # Manually patch K8s so cooldown is the only gate that should fire.
        api, _ = self._mock_k8s_deployment(spec_replicas=3, status_replicas=3)
        with patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ):
            self.Job._run_worker_deployment_scaler()
        api.patch_namespaced_deployment.assert_not_called()

    def test_h2_scale_up_ignores_cooldown(self):
        # H2 regression test: scale-UP must fire immediately even within
        # cooldown — bursts need fast response.
        self.ICP.set_param(
            "vegeta.worker_last_scale_change_utc",
            fields.Datetime.now().isoformat(),
        )
        self._make_jobs(500)  # desired=5
        api, _ = self._mock_k8s_deployment(spec_replicas=1, status_replicas=1)
        with patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ):
            self.Job._run_worker_deployment_scaler()
        self._assert_patched_replicas(api, 5)

    def test_h1_no_patch_when_status_already_at_desired(self):
        # H1 regression: scaler must use max(spec, status) as "current",
        # not just spec. Scenario: load=250 → desired=3, but a scale-down
        # is in flight (spec=2, status=3 because one old pod is still
        # draining). With max() logic, current=3 == desired=3 → no-op.
        # With the buggy spec-only logic, current=2 → would patch to 3
        # (redundant work + log noise + K8s API call) every cron tick
        # until the draining pod finished.
        self._make_jobs(250)
        api, _ = self._mock_k8s_deployment(spec_replicas=2, status_replicas=3)
        with patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ):
            self.Job._run_worker_deployment_scaler()
        api.patch_namespaced_deployment.assert_not_called()

    def test_h10_none_replicas_treated_as_zero(self):
        # H10 regression: a freshly-created Deployment can have
        # spec.replicas=None mid-rollout. The `or 0` guard prevents a
        # TypeError when comparing None to int.
        api, _ = self._mock_k8s_deployment(spec_replicas=None, status_replicas=None)
        with patch.multiple(
            "odoo.addons.vegeta.models.vegeta_job",
            K8S_AVAILABLE=True,
            _load_k8s_config=MagicMock(),
            k8s_client=MagicMock(AppsV1Api=MagicMock(return_value=api)),
        ):
            # Must not raise. Empty queue + current=0 < min=1 → scale UP to 1.
            self.Job._run_worker_deployment_scaler()
        self._assert_patched_replicas(api, 1)


class TestReconcileStaleHeartbeats(VegetaTestCase):
    """Two-gate recovery (CRITICAL bug C2 fix):
        - Normal gate: stale (>300s) AND heartbeat_failure_count > 3
        - Safety net:  grossly stale (>900s) — regardless of failure count
    Single-gate "stale alone triggers recovery" was unsafe: a saturated
    Postgres pool failed heartbeats silently and triggered double-processing.
    """

    def _make_active(self, *, age_seconds, job_name="worker-h-1",
                     state="generating", failure_count=0):
        stale_dt = fields.Datetime.now() - timedelta(seconds=age_seconds)
        return self._create_job(
            state=state,
            job_name=job_name,
            last_heartbeat=stale_dt,
            heartbeat_failure_count=failure_count,
        )

    def test_stale_with_high_failure_count_recovered(self):
        job = self._make_active(age_seconds=400, failure_count=5)
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertFalse(job.job_name)
        self.assertEqual(job.heartbeat_failure_count, 0)

    def test_stale_with_low_failure_count_NOT_recovered(self):
        # CRITICAL: this is the C2 incident — stale heartbeat alone must
        # NOT trigger recovery. Without the failure-count gate, a
        # transiently saturated Postgres pool causes false recovery and
        # the same job runs on two workers (double Bedrock spend).
        job = self._make_active(age_seconds=400, failure_count=2)
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.job_name, "worker-h-1")
        self.assertEqual(job.heartbeat_failure_count, 2)

    def test_grossly_stale_recovered_regardless_of_failure_count(self):
        # Safety net path: if even the failure-counter increment couldn't
        # write (DB very broken), the 15-min grossly-stale fallback still
        # catches the job. failure_count=0 simulates "nothing was ever
        # logged".
        job = self._make_active(age_seconds=1000, failure_count=0)
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertFalse(job.job_name)

    def test_fresh_heartbeat_untouched(self):
        job = self._make_active(age_seconds=60, failure_count=10)
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.job_name, "worker-h-1")
        self.assertEqual(job.heartbeat_failure_count, 10)

    def test_normal_gate_threshold_boundary(self):
        # FAILURE_THRESHOLD=3, so count=3 is NOT enough (must be > 3).
        below = self._make_active(
            age_seconds=400, job_name="worker-h-A", failure_count=3,
        )
        above = self._make_active(
            age_seconds=400, job_name="worker-h-B", failure_count=4,
        )
        self.Job._run_reconcile_prd_jobs()
        below.invalidate_recordset()
        above.invalidate_recordset()
        self.assertEqual(below.job_name, "worker-h-A")
        self.assertFalse(above.job_name)

    def test_grossly_stale_threshold_boundary(self):
        # GROSSLY_STALE_S=900, so age=899 is NOT enough on the safety
        # path; age=901 is. (Both have failure_count=0 to isolate the
        # grossly-stale gate.)
        below = self._make_active(
            age_seconds=899, job_name="worker-h-G1", failure_count=0,
        )
        above = self._make_active(
            age_seconds=901, job_name="worker-h-G2", failure_count=0,
        )
        self.Job._run_reconcile_prd_jobs()
        below.invalidate_recordset()
        above.invalidate_recordset()
        self.assertEqual(below.job_name, "worker-h-G1")
        self.assertFalse(above.job_name)

    def test_inprocess_sentinel_recovered_via_grossly_stale(self):
        job = self._make_active(
            age_seconds=1000, job_name="inprocess", failure_count=0,
        )
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertFalse(job.job_name)

    def test_legacy_k8s_job_name_recovered_via_grossly_stale(self):
        job = self._make_active(
            age_seconds=1000, job_name="vegeta-prd-99-abc", failure_count=0,
        )
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertFalse(job.job_name)

    def test_state_not_in_recovery_set_untouched(self):
        job = self._make_active(
            age_seconds=1000, state="done", failure_count=10,
        )
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.job_name, "worker-h-1")

    def test_empty_job_name_untouched(self):
        # Already queued for re-claim; reconcile must not re-process it.
        stale_dt = fields.Datetime.now() - timedelta(seconds=1000)
        job = self._create_job(
            state="generating", job_name=False, last_heartbeat=stale_dt,
            heartbeat_failure_count=10,
        )
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertFalse(job.job_name)

    def test_recovery_resets_failure_count(self):
        # After recovery, the next claim should start with a clean counter
        # so the new worker isn't immediately re-recovered.
        job = self._make_active(age_seconds=1000, failure_count=7)
        self.Job._run_reconcile_prd_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.heartbeat_failure_count, 0)


class TestInprocessDispatch(VegetaTestCase):
    def setUp(self):
        super().setUp()
        self.ICP.set_param("vegeta.prd_execution_mode", "inprocess")

    @contextmanager
    def _patch_cursor_commit(self):
        # Production code calls self.env.cr.commit() so the bg thread sees
        # the new job_name row. Odoo 19 forbids commit/rollback inside a
        # test (auto-rollback at teardown breaks otherwise), so neutralise
        # both on the live test cursor instance. The bg thread itself is
        # already mocked via _patch_submit_bg, so commit is unobservable
        # here and the assertion verifies the in-memory write directly.
        with patch.object(self.env.cr, "commit"), \
             patch.object(self.env.cr, "rollback"):
            yield

    def test_pending_job_stamped_with_sentinel_and_submitted(self):
        job = self._create_job(state="generating", job_name=False)
        with self._patch_cursor_commit(), self._patch_submit_bg() as mock_submit:
            self.Job._cron_dispatch_prd_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.job_name, "inprocess")
        mock_submit.assert_called_once()

    def test_already_claimed_job_skipped(self):
        self._create_job(state="generating", job_name="inprocess")
        with self._patch_cursor_commit(), self._patch_submit_bg() as mock_submit:
            self.Job._cron_dispatch_prd_jobs()
        mock_submit.assert_not_called()

    def test_cancelled_job_skipped(self):
        self._create_job(
            state="generating", job_name=False, cancel_requested=True,
        )
        with self._patch_cursor_commit(), self._patch_submit_bg() as mock_submit:
            self.Job._cron_dispatch_prd_jobs()
        mock_submit.assert_not_called()

    def test_worker_mode_takes_scaling_path_not_inprocess(self):
        self.ICP.set_param("vegeta.prd_execution_mode", "worker")
        self._create_job(state="generating", job_name=False)
        # worker mode goes through the scaler; with K8s unavailable the
        # scaler returns quietly. The key assertion is the in-process
        # path is NOT triggered (no _submit_bg, no job_name stamp).
        with self._patch_submit_bg() as mock_submit:
            with patch(
                "odoo.addons.vegeta.models.vegeta_job.K8S_AVAILABLE",
                False,
            ):
                self.Job._cron_dispatch_prd_jobs()
        mock_submit.assert_not_called()


class TestWorkerClaimSQL(VegetaTestCase):
    """The worker daemon's ``_claim_jobs`` is a thin SQL function.

    The race-safety contract is provided by Postgres
    (FOR UPDATE SKIP LOCKED); we verify only the visible side-effects:
    pending jobs get a ``job_name`` stamp, ``last_heartbeat`` is
    refreshed, and ``started_processing_at`` is set on the first claim.
    The actual SQL is duplicated here (kept short) to avoid importing
    ``worker.run_prd`` — that module boots Odoo at import time.
    """

    def _claim(self, batch, label="worker-test-1"):
        cr = self.env.cr
        cr.execute(
            """
            SELECT id FROM vegeta_job
             WHERE state = 'generating'
               AND (job_name IS NULL OR job_name = '')
               AND cancel_requested = FALSE
             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s
            """,
            (batch,),
        )
        ids = [r[0] for r in cr.fetchall()]
        if ids:
            cr.execute(
                """
                UPDATE vegeta_job
                   SET job_name = %s,
                       last_heartbeat = (now() AT TIME ZONE 'UTC'),
                       started_processing_at = COALESCE(
                           started_processing_at, (now() AT TIME ZONE 'UTC')
                       )
                 WHERE id = ANY(%s)
                """,
                (label, ids),
            )
        return ids

    def test_claim_stamps_job_name_and_heartbeat(self):
        a = self._create_job(state="generating", job_name=False)
        b = self._create_job(state="generating", job_name=False)
        claimed = self._claim(batch=10, label="worker-A-9")
        self.assertEqual(set(claimed), {a.id, b.id})
        a.invalidate_recordset()
        b.invalidate_recordset()
        self.assertEqual(a.job_name, "worker-A-9")
        self.assertEqual(b.job_name, "worker-A-9")
        self.assertTrue(a.started_processing_at)
        self.assertTrue(b.started_processing_at)

    def test_claim_skips_already_claimed(self):
        self._create_job(state="generating", job_name="worker-X-1")
        claimed = self._claim(batch=10)
        self.assertEqual(claimed, [])

    def test_claim_skips_cancelled(self):
        self._create_job(
            state="generating", job_name=False, cancel_requested=True,
        )
        claimed = self._claim(batch=10)
        self.assertEqual(claimed, [])

    def test_claim_respects_batch_size(self):
        for _ in range(5):
            self._create_job(state="generating", job_name=False)
        claimed = self._claim(batch=2)
        self.assertEqual(len(claimed), 2)

    def test_claim_preserves_started_processing_at_on_reclaim(self):
        # If reconcile clears job_name and worker re-claims, the
        # original started_processing_at must survive (COALESCE).
        from datetime import datetime, timezone
        original = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        job = self._create_job(
            state="generating",
            job_name=False,
            started_processing_at=original,
        )
        self._claim(batch=10, label="worker-Y-1")
        job.invalidate_recordset()
        self.assertEqual(
            job.started_processing_at, original,
            "started_processing_at must not be overwritten on re-claim",
        )
