from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import LeviathanTestCase


@tagged("post_install", "-at_install", "leviathan")
class TestActionStartTask(LeviathanTestCase):

    def test_picks_oldest_prd_ready_unassigned_task(self):
        a = self._create_job(prd_text="ready prd a")
        b = self._create_job(prd_text="ready prd b")
        action = self.Job.action_start_task()
        self.assertEqual(action["res_id"], a.id)
        a.invalidate_recordset()
        self.assertEqual(a.user_id, self.tasker)
        self.assertEqual(a.state, "done")
        b.invalidate_recordset()
        self.assertFalse(b.user_id)

    def test_raises_when_no_tasks(self):
        with self.assertRaises(UserError):
            self.Job.action_start_task()

    def test_category_filter(self):
        self._create_job(category_id=self.category.id, prd_text="ready plain")
        ct_job = self._create_job(category_id=self.category_ct.id, prd_text="ready ct")
        action = self.Job.with_context(
            start_task_category_id=self.category_ct.id,
        ).action_start_task()
        self.assertEqual(action["res_id"], ct_job.id)

    def test_done_shortcut_when_prd_text_present(self):
        job = self._create_job(prd_text="existing PRD body")
        self.Job.action_start_task()
        job.invalidate_recordset()
        self.assertEqual(job.state, "done")

    def test_skips_fresh_not_assigned_jobs_without_prd(self):
        self._create_job(url="https://fresh-only.com")
        ready = self._create_job(url="https://ready.com", prd_text="existing PRD body")
        action = self.Job.action_start_task()
        self.assertEqual(action["res_id"], ready.id)

    def test_bandwidth_limit_blocks_pickup(self):
        self._set_param("leviathan.max_jobs_per_user", "1")
        self._create_job(user_id=self.tasker.id)
        self._create_job(prd_text="ready prd")
        with self.assertRaises(UserError):
            self.Job.action_start_task()

    def test_bandwidth_zero_means_unlimited(self):
        self._set_param("leviathan.max_jobs_per_user", "0")
        for _ in range(3):
            self._create_job(user_id=self.tasker.id, state="extracting")
        new_job = self._create_job(prd_text="ready prd")
        self.Job.action_start_task()
        new_job.invalidate_recordset()
        self.assertEqual(new_job.user_id, self.tasker)


@tagged("post_install", "-at_install", "leviathan")
class TestActionReleaseTask(LeviathanTestCase):

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionResetSelected(LeviathanTestCase):

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionRun(LeviathanTestCase):

    def test_blocks_when_state_invalid(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        with self.assertRaises(UserError):
            job.action_run()

    def test_opens_rerun_wizard_when_extraction_data_present(self):
        job = self._create_job(user_id=self.tasker.id, prd_prompt="extracted")
        result = job.action_run()
        self.assertEqual(result["res_model"], "leviathan.rerun.wizard")

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionCancel(LeviathanTestCase):

    def test_cancels_running_job(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertTrue(job.cancel_requested)

    def test_blocks_when_not_running(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_cancel()


@tagged("post_install", "-at_install", "leviathan")
class TestActionDiscard(LeviathanTestCase):

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionReopen(LeviathanTestCase):

    def test_reopens_discarded_to_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        job.action_discard()
        job.action_reopen()
        self.assertEqual(job.state, "draft")

    def test_blocks_when_not_discarded(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_reopen()


@tagged("post_install", "-at_install", "leviathan")
class TestActionRetry(LeviathanTestCase):

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionMarkSubmitted(LeviathanTestCase):

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


@tagged("post_install", "-at_install", "leviathan")
class TestActionRetryFailedBatch(LeviathanTestCase):
    """Admin bulk-retry: runs the pipeline end-to-end on selected failed tasks.

    Routing rules verified here:
      - prd_prompt present → state=generating (skip extraction)
      - prd_prompt absent  → state=extracting (full pipeline via Lambda)
      - user_id present    → via_batch=False (result stays at done)
      - user_id absent     → via_batch=True  (auto-release back to pool)
      - non-failed tasks   → ignored (with notification message)
    """

    def test_with_prd_prompt_and_user_goes_generating_no_via_batch(self):
        job = self._create_job(
            user_id=self.tasker.id,
            state="failed",
            prd_prompt="extracted data",
            error_message="old error",
        )
        with self._patch_submit_bg():
            job.action_retry_failed_batch()
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.via_batch, "tasker present → must NOT release at end")
        self.assertFalse(job.error_message)
        self.assertEqual(job.user_id, self.tasker, "tasker preserved")

    def test_with_prd_prompt_no_user_goes_generating_via_batch_true(self):
        job = self._create_job(
            user_id=False, state="failed", prd_prompt="extracted",
        )
        with self._patch_submit_bg():
            job.action_retry_failed_batch()
        self.assertEqual(job.state, "generating")
        self.assertTrue(job.via_batch, "no tasker → must auto-release at end")

    def test_without_prd_prompt_with_user_re_extracts(self):
        self._set_param("leviathan.lambda_function_name", "test-function")
        job = self._create_job(
            user_id=self.tasker.id, state="failed",
            prd_text="stale prd", screenshot_keys=["a.png"],
        )
        with self._patch_submit_bg():
            job.action_retry_failed_batch()
        self.assertEqual(job.state, "extracting")
        self.assertFalse(job.prd_text, "stale results wiped for fresh extract")
        self.assertFalse(job.screenshot_keys, "stale assets wiped")
        self.assertEqual(job.user_id, self.tasker, "tasker preserved")
        self.assertFalse(job.via_batch)

    def test_re_extract_without_lambda_config_raises(self):
        # No leviathan.lambda_function_name set
        self._set_param("leviathan.lambda_function_name", "")
        job = self._create_job(user_id=self.tasker.id, state="failed")
        with self.assertRaises(UserError):
            job.action_retry_failed_batch()
        # Job unchanged on the validation error
        self.assertEqual(job.state, "failed")

    def test_non_failed_ignored(self):
        # Mix: 1 failed + 1 draft. Failed should retry; draft should be reported skipped.
        failed = self._create_job(
            user_id=self.tasker.id, state="failed", prd_prompt="extracted",
        )
        draft = self._create_job(user_id=self.tasker.id)  # state=draft via auto-promote
        with self._patch_submit_bg():
            result = (failed | draft).action_retry_failed_batch()
        self.assertEqual(failed.state, "generating")
        self.assertEqual(draft.state, "draft", "non-failed task untouched")
        # Notification message should mention skipped
        self.assertIn("ignored", result["params"]["message"])

    def test_empty_selection_raises(self):
        draft = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            draft.action_retry_failed_batch()


@tagged("post_install", "-at_install", "leviathan")
class TestActionRunBatchConcurrent(LeviathanTestCase):

    def test_raises_when_no_eligible(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_run_batch_concurrent()

    def test_requires_lambda_function_for_extract_path(self):
        job = self._create_job()
        self._set_param("leviathan.lambda_function_name", "")
        with self.assertRaises(UserError):
            job.action_run_batch_concurrent()

    def test_splits_extract_vs_generate(self):
        self._set_param("leviathan.lambda_function_name", "lev-extract")
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


@tagged("post_install", "-at_install", "leviathan")
class TestWatchdogCron(LeviathanTestCase):

    def test_marks_stale_extracting_jobs_failed(self):
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            user_id=self.tasker.id, state="extracting", last_heartbeat=old,
        )
        # Disable auto-retry so the watchdog marks it FAILED on the first hit.
        self._set_param("leviathan.watchdog_auto_retry_max", "0")
        with self._patch_cursor_commit():
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")
        self.assertIn("timed out", job.error_message)

    def test_marks_stale_generating_jobs_failed(self):
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            user_id=self.tasker.id, state="generating", last_heartbeat=old,
            started_processing_at=old,
        )
        self._set_param("leviathan.watchdog_auto_retry_max", "0")
        with self._patch_cursor_commit(), self._patch_submit_bg():
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")


    def test_fresh_jobs_untouched(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracting",
            last_heartbeat=fields.Datetime.now(),
        )
        self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "extracting")


@tagged("post_install", "-at_install", "leviathan")
class TestActionDownloadZip(LeviathanTestCase):

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
