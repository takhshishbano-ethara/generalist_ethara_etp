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
        old = fields.Datetime.now() - timedelta(minutes=120)
        job = self._create_job(
            user_id=self.tasker.id, state="generating", last_heartbeat=old,
        )
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
