import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

from odoo import fields
from odoo.tests.common import TransactionCase


def _make_k8s_job(repo_id, succeeded=0, failed=0, active=0):
    job = MagicMock()
    job.metadata.labels = {
        "repo-id": str(repo_id),
        "platform": "jaeger",
        "app.kubernetes.io/name": "jaeger-scrape",
    }
    job.metadata.name = f"jaeger-scrape-{repo_id}"
    job.status.succeeded = succeeded
    job.status.failed = failed
    job.status.active = active
    return job


def _make_k8s_job_list(jobs):
    job_list = MagicMock()
    job_list.items = jobs
    return job_list


def _make_pod(name):
    pod = MagicMock()
    pod.metadata.name = name
    return pod


def _make_pod_list(pods):
    pod_list = MagicMock()
    pod_list.items = pods
    return pod_list


def _patch_k8s(batch_return=None, core_return=None):
    """Return a context manager that patches kubernetes imports inside _run_reconcile_scrape_jobs.

    Since the function does `from kubernetes import client, config as k8s_config`
    inside the function body, we patch the kubernetes package modules directly.
    """
    mock_batch = MagicMock()
    if batch_return is not None:
        mock_batch.list_namespaced_job.return_value = batch_return

    mock_core = MagicMock()
    if core_return is not None:
        mock_core.list_namespaced_pod.return_value = core_return

    mock_client = MagicMock()
    mock_client.BatchV1Api.return_value = mock_batch
    mock_client.CoreV1Api.return_value = mock_core
    mock_client.ApiException = Exception

    mock_config = MagicMock()

    return (
        patch.dict("sys.modules", {
            "kubernetes": MagicMock(client=mock_client, config=mock_config),
            "kubernetes.client": mock_client,
            "kubernetes.config": mock_config,
        }),
        mock_batch,
        mock_core,
    )


class TestReconcileScrapeJobs(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.eks_namespace", "jaeger")

    def _create_repo(self, status="running", queued_minutes_ago=0):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/rec-org/rec-repo-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": status,
        })
        if queued_minutes_ago:
            past = fields.Datetime.now() - timedelta(minutes=queued_minutes_ago)
            repo.write({"scrape_queued_at": past})
        return repo

    def test_job_succeeded_marks_done(self):
        repo = self._create_repo("running")
        self.env["jaeger.instance"].create({
            "name": f"rec-org__rec-repo-{repo.id}-1",
            "repository_id": repo.id,
            "org": "rec-org",
            "repo": f"rec-repo-{repo.id}",
            "pr_number": 1,
        })

        job = _make_k8s_job(repo.id, succeeded=1)
        patcher, mock_batch, _ = _patch_k8s(batch_return=_make_k8s_job_list([job]))
        with patcher:
            repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "done")

    def test_job_succeeded_no_instances_triggers_recovery(self):
        repo = self._create_repo("running")

        job = _make_k8s_job(repo.id, succeeded=1)
        patcher, mock_batch, _ = _patch_k8s(batch_return=_make_k8s_job_list([job]))
        with patcher:
            with patch.object(type(repo), "_recover_instances_from_s3") as mock_recover:
                repo._run_reconcile_scrape_jobs()
                mock_recover.assert_called_once()

        self.assertEqual(repo.pr_collection_status, "done")

    def test_job_succeeded_recovery_fails_skips_done(self):
        repo = self._create_repo("running")

        job = _make_k8s_job(repo.id, succeeded=1)
        patcher, _, _ = _patch_k8s(batch_return=_make_k8s_job_list([job]))
        with patcher:
            with patch.object(type(repo), "_recover_instances_from_s3",
                              side_effect=Exception("S3 down")):
                repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "running")

    def test_job_failed_writes_error_with_logs(self):
        repo = self._create_repo("running")

        job = _make_k8s_job(repo.id, failed=1)
        pod = _make_pod(f"jaeger-scrape-{repo.id}-xyz")
        patcher, mock_batch, mock_core = _patch_k8s(
            batch_return=_make_k8s_job_list([job]),
            core_return=_make_pod_list([pod]),
        )
        mock_core.read_namespaced_pod_log.return_value = "OOMKilled"

        with patcher:
            repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "failed")
        self.assertIn("reconciliation", repo.error_message)
        self.assertIn("OOMKilled", repo.error_message)

    def test_job_missing_over_5min_marks_failed(self):
        repo = self._create_repo("queued", queued_minutes_ago=10)

        patcher, _, _ = _patch_k8s(batch_return=_make_k8s_job_list([]))
        with patcher:
            repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "failed")
        self.assertIn("not found", repo.error_message)

    def test_job_missing_under_5min_no_change(self):
        repo = self._create_repo("queued", queued_minutes_ago=2)

        patcher, _, _ = _patch_k8s(batch_return=_make_k8s_job_list([]))
        with patcher:
            repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "queued")

    def test_job_still_active_no_change(self):
        repo = self._create_repo("running")

        job = _make_k8s_job(repo.id, active=1)
        patcher, _, _ = _patch_k8s(batch_return=_make_k8s_job_list([job]))
        with patcher:
            repo._run_reconcile_scrape_jobs()

        self.assertEqual(repo.pr_collection_status, "running")

    def test_already_done_not_touched(self):
        repo = self._create_repo("running")
        repo.write({"pr_collection_status": "done"})

        patcher, mock_batch, _ = _patch_k8s(batch_return=_make_k8s_job_list([]))
        with patcher:
            repo._run_reconcile_scrape_jobs()
            mock_batch.list_namespaced_job.assert_not_called()


class TestWatchdogStaleScrapes(TransactionCase):

    def _create_repo(self, status, heartbeat_minutes_ago=None):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/wd-org/wd-repo-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": status,
        })
        if heartbeat_minutes_ago is not None:
            past = fields.Datetime.now() - timedelta(minutes=heartbeat_minutes_ago)
            repo.write({"last_heartbeat": past})
        return repo

    def test_stale_running_no_heartbeat_marked_failed(self):
        repo = self._create_repo("running")
        repo._run_watchdog_stale_scrapes()
        self.assertEqual(repo.pr_collection_status, "failed")
        self.assertIn("no heartbeat", repo.error_message)

    def test_stale_running_old_heartbeat_marked_failed(self):
        repo = self._create_repo("running", heartbeat_minutes_ago=90)
        repo._run_watchdog_stale_scrapes()
        self.assertEqual(repo.pr_collection_status, "failed")

    def test_recent_heartbeat_not_touched(self):
        repo = self._create_repo("running", heartbeat_minutes_ago=5)
        repo._run_watchdog_stale_scrapes()
        self.assertEqual(repo.pr_collection_status, "running")

    def test_queued_status_not_affected(self):
        repo = self._create_repo("queued")
        repo._run_watchdog_stale_scrapes()
        self.assertEqual(repo.pr_collection_status, "queued")

    def test_stuck_validation_reset(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/wd-org/stuck-val-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "crawl_status": "running",
        })
        self.env.flush_all()
        past = fields.Datetime.now() - timedelta(minutes=15)
        self.env.cr.execute(
            "UPDATE jaeger_repository SET write_date = %s WHERE id = %s",
            [past, repo.id],
        )
        self.env.invalidate_all()

        repo._run_watchdog_stale_scrapes()
        self.env.invalidate_all()
        self.assertEqual(repo.crawl_status, "failed")
        self.assertIn("validation appears stuck", repo.error_message)

    def test_recent_validation_not_reset(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/wd-org/fresh-val-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "crawl_status": "running",
        })
        repo._run_watchdog_stale_scrapes()
        self.assertEqual(repo.crawl_status, "running")


class TestWatchdogStaleBuilds(TransactionCase):

    def test_stuck_build_reset_to_pending(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/bld-org/bld-repo-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage3",
            "docker_build_status": "building",
        })
        inst = self.env["jaeger.instance"].create({
            "name": f"bld-org__bld-repo-{count}-1",
            "repository_id": repo.id,
            "org": "bld-org",
            "repo": f"bld-repo-{count}",
            "pr_number": 1,
            "docker_build_status": "building",
        })

        self.env.flush_all()
        past = fields.Datetime.now() - timedelta(hours=3)
        self.env.cr.execute(
            "UPDATE jaeger_repository SET write_date = %s WHERE id = %s",
            [past, repo.id],
        )
        self.env.invalidate_all()

        repo._run_watchdog_stale_builds()
        self.env.invalidate_all()

        self.assertEqual(repo.docker_build_status, "pending")
        self.assertEqual(inst.docker_build_status, "pending")
        self.assertIn("stuck", repo.error_message)

    def test_recent_build_not_reset(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/bld-org/bld-fresh-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage3",
            "docker_build_status": "building",
        })
        repo._run_watchdog_stale_builds()
        self.assertEqual(repo.docker_build_status, "building")


class TestAutoAdvanceStages(TransactionCase):

    def _run_auto_advance(self):
        with patch.object(type(self.env["ir.cron"]), "_commit_progress"):
            self.env["jaeger.repository"]._cron_auto_advance_stages()

    def test_auto_advance_stage2_to_stage3(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/adv-org/adv-repo-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": "done",
        })
        self.env["jaeger.instance"].create({
            "name": f"adv-org__adv-repo-{count}-1",
            "repository_id": repo.id,
            "org": "adv-org",
            "repo": f"adv-repo-{count}",
            "pr_number": 1,
        })

        self._run_auto_advance()
        repo.invalidate_recordset()
        self.assertEqual(repo.current_stage, "stage3")

    def test_auto_advance_blocked_by_gate(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/adv-org/adv-block-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": "running",
        })

        self._run_auto_advance()
        repo.invalidate_recordset()
        self.assertEqual(repo.current_stage, "stage2")

    def test_auto_advance_blocked_by_terminal_state(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/adv-org/adv-term-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": "done",
            "terminal_state": "no_valid_prs",
        })

        self._run_auto_advance()
        repo.invalidate_recordset()
        self.assertEqual(repo.current_stage, "stage2")

    def test_auto_advance_skips_done_stage(self):
        count = self.env["jaeger.repository"].search_count([])
        repo = self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/adv-org/adv-done-{count}",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "done",
        })

        self._run_auto_advance()
        repo.invalidate_recordset()
        self.assertEqual(repo.current_stage, "done")
