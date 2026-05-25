from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import GohanTestCase


@tagged("post_install", "-at_install", "gohan")
class TestActionStartTask(GohanTestCase):

    def test_picks_oldest_unassigned_task(self):
        a = self._create_job()
        b = self._create_job()
        action = self.Job.action_start_task()
        self.assertEqual(action["res_id"], a.id)
        a.invalidate_recordset()
        self.assertEqual(a.user_id, self.tasker)
        self.assertEqual(a.state, "draft")
        b.invalidate_recordset()
        self.assertFalse(b.user_id)

    def test_raises_when_no_tasks(self):
        with self.assertRaises(UserError):
            self.Job.action_start_task()

    def test_category_filter(self):
        self._create_job(category_id=self.category.id)
        ct_job = self._create_job(category_id=self.category_ct.id)
        action = self.Job.with_context(
            start_task_category_id=self.category_ct.id,
        ).action_start_task()
        self.assertEqual(action["res_id"], ct_job.id)

    def test_done_shortcut_when_prd_text_present(self):
        job = self._create_job(prd_text="existing PRD body")
        self.Job.action_start_task()
        job.invalidate_recordset()
        self.assertEqual(job.state, "done")

    def test_bandwidth_limit_blocks_pickup(self):
        self._set_param("gohan.max_jobs_per_user", "1")
        self._create_job(user_id=self.tasker.id)
        self._create_job()
        with self.assertRaises(UserError):
            self.Job.action_start_task()

    def test_bandwidth_zero_means_unlimited(self):
        self._set_param("gohan.max_jobs_per_user", "0")
        for _ in range(3):
            self._create_job(user_id=self.tasker.id, state="extracting")
        new_job = self._create_job()
        self.Job.action_start_task()
        new_job.invalidate_recordset()
        self.assertEqual(new_job.user_id, self.tasker)


@tagged("post_install", "-at_install", "gohan")
class TestActionReleaseTask(GohanTestCase):

    def test_releases_from_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        job.action_release_task()
        self.assertEqual(job.state, "not_assigned")
        self.assertFalse(job.user_id)

    def test_blocks_on_invalid_state(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        with self.assertRaises(UserError):
            job.action_release_task()

    def test_clears_error_message(self):
        job = self._create_job(
            user_id=self.tasker.id, state="failed",
            error_message="boom", cancel_requested=True,
        )
        job.action_release_task()
        self.assertFalse(job.error_message)
        self.assertFalse(job.cancel_requested)


@tagged("post_install", "-at_install", "gohan")
class TestActionResetSelected(GohanTestCase):

    def test_resets_in_progress_with_cancel_flag(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        job.action_reset_selected()
        self.assertEqual(job.state, "not_assigned")
        self.assertTrue(job.cancel_requested)
        self.assertFalse(job.user_id)

    def test_skips_submitted(self):
        job = self._create_job(user_id=self.tasker.id, state="submitted")
        with self.assertRaises(UserError):
            job.action_reset_selected()

    def test_mixed_selection_reports_skipped(self):
        ok = self._create_job(user_id=self.tasker.id)
        sub = self._create_job(user_id=self.tasker.id, state="submitted")
        result = (ok | sub).action_reset_selected()
        self.assertEqual(result["tag"], "display_notification")
        ok.invalidate_recordset()
        self.assertEqual(ok.state, "not_assigned")
        sub.invalidate_recordset()
        self.assertEqual(sub.state, "submitted")


@tagged("post_install", "-at_install", "gohan")
class TestActionRun(GohanTestCase):

    def test_blocks_when_state_invalid(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        with self.assertRaises(UserError):
            job.action_run()

    def test_opens_rerun_wizard_when_extraction_data_present(self):
        job = self._create_job(user_id=self.tasker.id, prd_prompt="extracted")
        result = job.action_run()
        self.assertEqual(result["res_model"], "gohan.rerun.wizard")

    def test_runs_pipeline_and_dispatches_extraction(self):
        job = self._create_job(user_id=self.tasker.id)
        with self._patch_submit_bg() as mock_submit:
            job.action_run()
            self.env.cr.flush()
        job.invalidate_recordset()
        self.assertEqual(job.state, "extracting")
        self.assertTrue(job.started_at)

    def test_auto_assigns_unassigned_job(self):
        job = self._create_job()
        with self._patch_submit_bg():
            job.action_run()
        job.invalidate_recordset()
        self.assertEqual(job.user_id, self.tasker)
        self.assertEqual(job.state, "extracting")


@tagged("post_install", "-at_install", "gohan")
class TestActionCancel(GohanTestCase):

    def test_cancels_running_job(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertTrue(job.cancel_requested)

    def test_blocks_when_not_running(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_cancel()


@tagged("post_install", "-at_install", "gohan")
class TestActionDiscard(GohanTestCase):

    def test_discards_from_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        job.action_discard()
        self.assertEqual(job.state, "discarded")
        self.assertTrue(job.completed_at)

    def test_discards_in_progress_sets_cancel_flag(self):
        job = self._create_job(user_id=self.tasker.id, state="generating")
        job.action_discard()
        self.assertEqual(job.state, "discarded")
        self.assertTrue(job.cancel_requested)

    def test_discarded_twice_blocked(self):
        job = self._create_job(user_id=self.tasker.id)
        job.action_discard()
        with self.assertRaises(UserError):
            job.action_discard()


@tagged("post_install", "-at_install", "gohan")
class TestActionReopen(GohanTestCase):

    def test_reopens_discarded_to_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        job.action_discard()
        job.action_reopen()
        self.assertEqual(job.state, "draft")

    def test_blocks_when_not_discarded(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_reopen()


@tagged("post_install", "-at_install", "gohan")
class TestActionRetry(GohanTestCase):

    def test_blocks_when_not_failed(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_retry()

    def test_with_prd_prompt_skips_extraction(self):
        job = self._create_job(
            user_id=self.tasker.id, state="failed",
            prd_prompt="extracted", error_message="boom",
        )
        with self._patch_submit_bg():
            job.action_retry()
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.error_message)

    def test_without_prd_prompt_resets_to_draft(self):
        job = self._create_job(
            user_id=self.tasker.id, state="failed",
            error_message="boom",
        )
        job.action_retry()
        self.assertEqual(job.state, "draft")
        self.assertFalse(job.error_message)


@tagged("post_install", "-at_install", "gohan")
class TestActionMarkSubmitted(GohanTestCase):

    def test_requires_done(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_mark_submitted()

    def test_requires_qc_verdict(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done", qc_verdict=False,
        )
        with self.assertRaises(UserError):
            job.action_mark_submitted()

    def test_marks_when_qc_present(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done", qc_verdict="shippable",
        )
        job.action_mark_submitted()
        self.assertEqual(job.state, "submitted")


@tagged("post_install", "-at_install", "gohan")
class TestActionRunBatchConcurrent(GohanTestCase):

    def test_raises_when_no_eligible(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_run_batch_concurrent()

    def test_requires_lambda_function_for_extract_path(self):
        job = self._create_job()
        self._set_param("gohan.lambda_function_name", "")
        with self.assertRaises(UserError):
            job.action_run_batch_concurrent()

    def test_splits_extract_vs_generate(self):
        self._set_param("gohan.lambda_function_name", "lev-extract")
        a = self._create_job()
        b = self._create_job(prd_prompt="already extracted")
        with self._patch_submit_bg():
            result = (a | b).action_run_batch_concurrent()
            self.env.cr.flush()
        a.invalidate_recordset()
        b.invalidate_recordset()
        self.assertEqual(a.state, "extracting")
        self.assertEqual(b.state, "generating")
        self.assertEqual(result["tag"], "display_notification")


@tagged("post_install", "-at_install", "gohan")
class TestWatchdogCron(GohanTestCase):

    def test_marks_stale_extracting_jobs_failed(self):
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            user_id=self.tasker.id, state="extracting", last_heartbeat=old,
        )
        self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")
        self.assertIn("timed out", job.error_message)

    def test_marks_stale_generating_jobs_failed(self):
        # A CLAIMED job (started_processing_at set) gone quiet past the fail
        # threshold = a genuinely stuck running worker. Watchdog Path 2 fails
        # it. Contrast with an *unclaimed* generating job, which the Fix 4
        # deferred pass re-submits instead (see test below).
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            user_id=self.tasker.id, state="generating",
            last_heartbeat=old, started_processing_at=old,
        )
        self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")

    def test_deferred_unclaimed_generating_job_resubmitted(self):
        # Fix 4 recovery: a generating job with no worker
        # (started_processing_at = False) idle past the short deferred
        # threshold is RE-SUBMITTED, not failed.
        stale = fields.Datetime.now() - timedelta(minutes=20)
        job = self._create_job(
            user_id=self.tasker.id, state="generating", last_heartbeat=stale,
        )
        with patch.object(self.env.cr, "postcommit") as mock_pc:
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        # Not failed — left in generating for the re-submitted worker.
        self.assertEqual(job.state, "generating")
        # Heartbeat pulsed forward (natural backoff against re-rejection).
        self.assertGreater(job.last_heartbeat, stale)
        # Exactly one re-submission queued on postcommit.
        self.assertTrue(mock_pc.add.called)
        resubmit_fn = mock_pc.add.call_args[0][0]
        # Invoking it routes through _submit_bg with a watchdog-resubmit label.
        with self._patch_submit_bg() as mock_submit:
            resubmit_fn()
        self.assertTrue(mock_submit.called)
        self.assertIn("watchdog-resubmit", mock_submit.call_args[0][0])

    def test_queued_generating_job_within_threshold_untouched(self):
        # An unclaimed generating job only 5 min idle is presumed genuinely
        # queued in the pool — well within drain time. The deferred pass must
        # NOT re-submit it (that is the "don't disturb queued jobs" guard).
        recent = fields.Datetime.now() - timedelta(minutes=5)
        job = self._create_job(
            user_id=self.tasker.id, state="generating", last_heartbeat=recent,
        )
        with patch.object(self.env.cr, "postcommit") as mock_pc:
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertFalse(mock_pc.add.called)

    def test_watchdog_routes_extracting_vs_generating(self):
        # One run, two jobs: a stale extracting job goes to Path 1 (failed,
        # no S3 rescue), while a stale unclaimed generating job goes to the
        # Fix 4 deferred pass (re-submitted, still generating). Proves the
        # deferred pass never touches extracting state.
        old = fields.Datetime.now() - timedelta(minutes=120)
        extracting = self._create_job(
            user_id=self.tasker.id, state="extracting", last_heartbeat=old,
        )
        generating = self._create_job(
            user_id=self.tasker.id, state="generating", last_heartbeat=old,
        )
        with patch.object(self.env.cr, "postcommit"):
            self.Job._cron_watchdog_stuck_jobs()
        extracting.invalidate_recordset()
        generating.invalidate_recordset()
        self.assertEqual(extracting.state, "failed")
        self.assertEqual(generating.state, "generating")

    def test_fresh_jobs_untouched(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracting",
            last_heartbeat=fields.Datetime.now(),
        )
        self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "extracting")


@tagged("post_install", "-at_install", "gohan")
class TestActionDownloadZip(GohanTestCase):

    def test_requires_prd_text(self):
        job = self._create_job(user_id=self.tasker.id, state="done")
        with self.assertRaises(UserError):
            job.action_download_zip()

    def test_builds_attachment(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done",
            prd_text="# Demo PRD\nBody.",
            qc_report="# QC\nVerdict: SHIPPABLE",
        )
        result = job.action_download_zip()
        self.assertEqual(result["type"], "ir.actions.act_url")
        self.assertIn("/web/content/", result["url"])


@tagged("post_install", "-at_install", "gohan")
class TestStartedProcessingReset(GohanTestCase):
    """Fix 4: re-trigger actions must clear started_processing_at so the PRD
    worker's re-entry guard re-claims the job (a stale claim from a prior run
    would otherwise make the guard skip the new run)."""

    def test_batch_generate_path_resets_started_processing_at(self):
        self._set_param("gohan.lambda_function_name", "lev-extract")
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            prd_prompt="already extracted", started_processing_at=old,
        )
        with self._patch_submit_bg():
            job.action_run_batch_concurrent()
            self.env.cr.flush()
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.started_processing_at)


@tagged("post_install", "-at_install", "gohan")
class TestAdmissionControl(GohanTestCase):
    """Fix 4: _submit_bg refuses work once the in-flight + queued admission
    cap is reached, instead of growing the pool queue without bound."""

    def test_submit_bg_rejects_when_admission_full(self):
        from odoo.addons.gohan.models import gohan_job

        sem = gohan_job._ADMISSION_SEMAPHORE
        drained = 0
        try:
            while sem.acquire(timeout=0):
                drained += 1
            self.assertGreater(drained, 0)
            ran = []
            result = gohan_job._submit_bg(
                "test-rejected", lambda: ran.append(1)
            )
            # Rejected: returns None and the callable never runs.
            self.assertIsNone(result)
            self.assertEqual(ran, [])
        finally:
            for _ in range(drained):
                sem.release()


@tagged("post_install", "-at_install", "gohan")
class TestHeartbeatManager(GohanTestCase):
    """Fix 3: the shared heartbeat manager's registry mechanics."""

    def test_register_tracks_jobs_and_starts_thread(self):
        from odoo.addons.gohan.models.gohan_job import _HeartbeatManager

        # Long interval so the daemon never ticks (no DB) during the test.
        mgr = _HeartbeatManager(interval=3600)
        try:
            mgr.register("testdb", 101)
            self.assertIn(("testdb", 101), mgr._active)
            self.assertIsNotNone(mgr._thread)
            self.assertTrue(mgr._thread.is_alive())
            mgr.register("testdb", 102)
            self.assertEqual(len(mgr._active), 2)
            mgr.unregister("testdb", 101)
            self.assertNotIn(("testdb", 101), mgr._active)
            self.assertIn(("testdb", 102), mgr._active)
        finally:
            mgr._stop_event.set()

    def test_register_is_reference_counted(self):
        # Two workers can track the same job (Fix 4 re-submission race,
        # poller/PRD overlap). The job must stay tracked until the LAST
        # worker unregisters — not the first.
        from odoo.addons.gohan.models.gohan_job import _HeartbeatManager

        mgr = _HeartbeatManager(interval=3600)
        try:
            mgr.register("db", 1)
            mgr.register("db", 1)
            self.assertIn(("db", 1), mgr._active)
            mgr.unregister("db", 1)
            self.assertIn(("db", 1), mgr._active)  # one worker still on it
            mgr.unregister("db", 1)
            self.assertNotIn(("db", 1), mgr._active)  # last worker left
        finally:
            mgr._stop_event.set()

    def test_register_ignores_blank_keys(self):
        from odoo.addons.gohan.models.gohan_job import _HeartbeatManager

        mgr = _HeartbeatManager(interval=3600)
        mgr.register("", 1)
        mgr.register("db", 0)
        mgr.register("db", None)
        self.assertEqual(len(mgr._active), 0)
        # No registration happened, so no daemon thread was started.
        self.assertIsNone(mgr._thread)


@tagged("post_install", "-at_install", "gohan")
class TestStaggeredFanout(GohanTestCase):
    """Fix 5: the batch fan-out dispatches Lambda invokes in time-spaced
    waves rather than all at once."""

    def test_fanout_dispatches_in_waves(self):
        from odoo.addons.gohan.models import gohan_job

        wave_size = gohan_job._FANOUT_WAVE_SIZE
        n = wave_size * 2 + 5  # spans 3 waves -> expect 2 inter-wave sleeps
        record_ids = list(range(1, n + 1))
        record_urls = {rid: "https://example.com" for rid in record_ids}
        config = {
            "batch_concurrency": 10,
            "function_name": "fn",
            "region": "us-east-1",
            "access_key_id": "k",
            "secret_access_key": "s",
            "local_url": "",
        }
        n_waves = (n + wave_size - 1) // wave_size
        expected_sleeps = n_waves - 1

        with patch(
            "odoo.addons.gohan.services.extraction_service.trigger_extraction",
            return_value={"success": True},
        ) as mock_trigger, patch(
            "odoo.addons.gohan.models.gohan_job.time.sleep",
        ) as mock_sleep:
            self.Job._fanout_batch_extraction(
                self.env.cr.dbname, record_ids, record_urls,
                "https://example.com/webhook", config,
            )

        self.assertEqual(mock_trigger.call_count, n)
        self.assertEqual(mock_sleep.call_count, expected_sleeps)


@tagged("post_install", "-at_install", "gohan")
class TestStagedPipeline(GohanTestCase):
    """Per-stage pipeline: from the 'extracted' review gate the tasker can
    step Generate -> Score -> Run QC, parking at 'generated' and 'scored'."""

    def test_stage_generate_from_extracted(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracted",
            prd_prompt="extracted website data",
        )
        with self._patch_submit_bg():
            job.action_stage_generate()
        job.invalidate_recordset()
        # Synchronous part: job advances to generating, claim token cleared.
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.started_processing_at)

    def test_stage_generate_rejects_wrong_state(self):
        job = self._create_job(user_id=self.tasker.id, state="draft")
        with self.assertRaises(UserError):
            job.action_stage_generate()

    def test_stage_score_from_generated(self):
        # action_stage_score runs the rubric inline (no Bedrock) — fully
        # exercisable end to end in a TransactionCase.
        job = self._create_job(
            user_id=self.tasker.id, state="generated",
            prd_text="# PRD\n\n## 1. Overview\nA product.\n\n## 2. Goals\nShip it.",
        )
        job.action_stage_score()
        job.invalidate_recordset()
        self.assertEqual(job.state, "scored")
        self.assertIsNotNone(job.score)
        self.assertTrue(job.grade)
        self.assertTrue(job.score_report_json)

    def test_stage_score_rejects_wrong_state(self):
        job = self._create_job(user_id=self.tasker.id, state="extracted")
        with self.assertRaises(UserError):
            job.action_stage_score()

    def test_stage_qc_from_scored(self):
        job = self._create_job(
            user_id=self.tasker.id, state="scored",
            prd_text="# PRD\nBody.", qc_verdict="shippable",
        )
        with self._patch_submit_bg():
            job.action_stage_qc()
        job.invalidate_recordset()
        # `scoring` doubles as the QC-running state; prior verdict cleared.
        self.assertEqual(job.state, "scoring")
        self.assertFalse(job.qc_verdict)

    def test_stage_qc_rejects_wrong_state(self):
        job = self._create_job(user_id=self.tasker.id, state="generated",
                               prd_text="# PRD\nBody.")
        with self.assertRaises(UserError):
            job.action_stage_qc()

    def test_cancel_resets_parked_generated_job(self):
        job = self._create_job(user_id=self.tasker.id, state="generated")
        job.action_cancel()
        job.invalidate_recordset()
        self.assertEqual(job.state, "draft")

    def test_cancel_resets_parked_scored_job(self):
        job = self._create_job(user_id=self.tasker.id, state="scored")
        job.action_cancel()
        job.invalidate_recordset()
        self.assertEqual(job.state, "draft")
