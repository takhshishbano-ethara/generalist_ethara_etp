"""Tests for the staged manual pipeline (ported from leviathan a544c2e09).

Covers the auto_continue gating, the four staged actions
(action_stage_extract / _generate / _score / _qc), the parked-state Cancel /
Reset behaviour, and the watchdog's staged-job recovery.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import VegetaTestCase


@tagged("post_install", "-at_install", "vegeta")
class TestStagedPipelineStates(VegetaTestCase):

    def test_new_states_registered(self):
        states = dict(self.Job._fields["state"].selection)
        for key in ("extracted", "generated", "scored", "qc_running"):
            self.assertIn(key, states)

    def test_auto_continue_defaults_true(self):
        job = self._create_job()
        self.assertTrue(job.auto_continue)


@tagged("post_install", "-at_install", "vegeta")
class TestAutoContinueGating(VegetaTestCase):

    def test_action_run_keeps_auto_continue_true(self):
        job = self._create_job(user_id=self.tasker.id)
        with self._patch_submit_bg():
            job.action_run()
        job.invalidate_recordset()
        self.assertEqual(job.state, "extracting")
        self.assertTrue(job.auto_continue)

    def test_stage_extract_sets_auto_continue_false(self):
        job = self._create_job(user_id=self.tasker.id)
        with self._patch_submit_bg():
            job.action_stage_extract()
        job.invalidate_recordset()
        self.assertEqual(job.state, "extracting")
        self.assertFalse(job.auto_continue)
        self.assertTrue(job.started_at)

    def test_stage_extract_blocks_when_state_invalid(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        with self.assertRaises(UserError):
            job.action_stage_extract()

    def test_stage_extract_auto_assigns_unassigned_job(self):
        job = self._create_job()
        with self._patch_submit_bg():
            job.action_stage_extract()
        job.invalidate_recordset()
        self.assertEqual(job.user_id, self.tasker)


@tagged("post_install", "-at_install", "vegeta")
class TestActionStageGenerate(VegetaTestCase):

    def test_blocks_when_not_extracted(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_stage_generate()

    def test_blocks_when_no_prd_prompt(self):
        job = self._create_job(user_id=self.tasker.id, state="extracted")
        with self.assertRaises(UserError):
            job.action_stage_generate()

    def test_transitions_to_generating(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracted",
            prd_prompt="extracted website data",
        )
        with self._patch_submit_bg():
            job.action_stage_generate()
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.started_processing_at)


@tagged("post_install", "-at_install", "vegeta")
class TestActionStageScore(VegetaTestCase):
    """action_stage_score is synchronous — it runs the real rubric scorer."""

    def test_blocks_when_not_generated(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_stage_score()

    def test_blocks_when_no_prd_text(self):
        job = self._create_job(user_id=self.tasker.id, state="generated")
        with self.assertRaises(UserError):
            job.action_stage_score()

    def test_scores_and_parks_at_scored(self):
        job = self._create_job(
            user_id=self.tasker.id, state="generated",
            prd_text="# PRD\n\n## Overview\nThis is a product spec body.",
        )
        job.action_stage_score()
        self.assertEqual(job.state, "scored")
        self.assertTrue(job.grade)
        self.assertTrue(job.score_report_json)
        self.assertIn("total_score", job.score_report_json)


@tagged("post_install", "-at_install", "vegeta")
class TestActionStageQc(VegetaTestCase):

    def test_blocks_when_not_scored(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_stage_qc()

    def test_blocks_when_no_prd_text(self):
        job = self._create_job(user_id=self.tasker.id, state="scored")
        with self.assertRaises(UserError):
            job.action_stage_qc()

    def test_transitions_to_qc_running(self):
        job = self._create_job(
            user_id=self.tasker.id, state="scored", prd_text="# PRD body",
        )
        with self._patch_submit_bg():
            job.action_stage_qc()
        job.invalidate_recordset()
        self.assertEqual(job.state, "qc_running")


@tagged("post_install", "-at_install", "vegeta")
class TestActionCancelStaged(VegetaTestCase):
    """Cancel / Reset walks parked staged states back to Draft without
    setting cancel_requested (no running thread); running states still flag."""

    def test_cancel_parked_extracted(self):
        job = self._create_job(user_id=self.tasker.id, state="extracted")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertFalse(job.cancel_requested)

    def test_cancel_parked_generated(self):
        job = self._create_job(user_id=self.tasker.id, state="generated")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertFalse(job.cancel_requested)

    def test_cancel_parked_scored(self):
        job = self._create_job(user_id=self.tasker.id, state="scored")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertFalse(job.cancel_requested)

    def test_cancel_running_qc_running_sets_flag(self):
        job = self._create_job(user_id=self.tasker.id, state="qc_running")
        job.action_cancel()
        self.assertEqual(job.state, "draft")
        self.assertTrue(job.cancel_requested)

    def test_cancel_blocks_from_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        with self.assertRaises(UserError):
            job.action_cancel()


@tagged("post_install", "-at_install", "vegeta")
class TestWatchdogStagedRecovery(VegetaTestCase):
    """Watchdog now covers qc_running and re-stages stuck manual jobs
    instead of failing them."""

    @classmethod
    def _stale(cls):
        return fields.Datetime.now() - timedelta(minutes=120)

    def test_auto_generating_job_marked_failed(self):
        job = self._create_job(
            user_id=self.tasker.id, state="generating", auto_continue=True,
            started_processing_at=self._stale(), last_heartbeat=self._stale(),
        )
        self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "failed")

    def test_staged_generating_job_restaged_not_failed(self):
        job = self._create_job(
            user_id=self.tasker.id, state="generating", auto_continue=False,
            prd_prompt="extracted data",
            started_processing_at=self._stale(), last_heartbeat=self._stale(),
        )
        with self._patch_submit_bg():
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")
        self.assertFalse(job.started_processing_at)
        self.assertGreater(
            job.last_heartbeat, fields.Datetime.now() - timedelta(minutes=5),
        )

    def test_staged_qc_running_job_restaged_not_failed(self):
        job = self._create_job(
            user_id=self.tasker.id, state="qc_running", auto_continue=False,
            prd_text="# PRD body",
            started_processing_at=self._stale(), last_heartbeat=self._stale(),
        )
        with self._patch_submit_bg():
            self.Job._cron_watchdog_stuck_jobs()
        job.invalidate_recordset()
        self.assertEqual(job.state, "qc_running")
