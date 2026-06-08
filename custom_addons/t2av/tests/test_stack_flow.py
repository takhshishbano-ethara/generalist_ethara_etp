import unittest
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@unittest.skip("Stack/Reviewer mode hidden in 19.0.1.19.0; re-enable when feature returns")
@tagged("post_install", "-at_install", "t2av")
class TestStackFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env["ir.config_parameter"].sudo()
        cls.Review = cls.env["t2av.video.review"]
        cls.Attempt = cls.env["t2av.attempt"]
        cls.Generation = cls.env["t2av.generation"]
        cls.SheetRow = cls.env["t2av.sequence.sheet.row"]
        cls.Wizard = cls.env["t2av.stack.review.wizard"]

        cls.group_reviewer = cls.env.ref("t2av.group_t2av_reviewer")
        cls.group_manager = cls.env.ref("t2av.group_t2av_manager")
        cls.group_admin = cls.env.ref("base.group_system")

        cls.reviewer_user = cls.env["res.users"].create({
            "name": "Stack Reviewer 1",
            "login": "stack_reviewer_1@t2av.test",
            "email": "stack_reviewer_1@t2av.test",
            "groups_id": [(6, 0, [cls.group_reviewer.id])],
        })
        cls.reviewer_user_2 = cls.env["res.users"].create({
            "name": "Stack Reviewer 2",
            "login": "stack_reviewer_2@t2av.test",
            "email": "stack_reviewer_2@t2av.test",
            "groups_id": [(6, 0, [cls.group_reviewer.id])],
        })
        cls.manager_user = cls.env["res.users"].create({
            "name": "Stack Manager",
            "login": "stack_manager@t2av.test",
            "email": "stack_manager@t2av.test",
            "groups_id": [(6, 0, [cls.group_manager.id])],
        })

    def setUp(self):
        super().setUp()
        self.ICP.set_param("t2av.enable_gemini_qc", "False")

    def _make_job(self, **vals):
        defaults = {
            "prompt": "A cat surfing in slow motion",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        }
        defaults.update(vals)
        return self.Generation.create(defaults)

    def _spawn_attempt(self, job, attempt_number=1, state="done", **overrides):
        vals = {
            "job_id": job.id,
            "attempt_number": attempt_number,
            "prompt": overrides.pop("prompt", job.prompt),
            "negative_prompt": overrides.pop("negative_prompt", job.negative_prompt or False),
            "duration": overrides.pop("duration", job.duration),
            "resolution": overrides.pop("resolution", job.resolution),
            "aspect_ratio": overrides.pop("aspect_ratio", job.aspect_ratio),
            "seed": overrides.pop("seed", job.seed or 0),
            "generate_audio": overrides.pop("generate_audio", job.generate_audio),
            "model_name": overrides.pop("model_name", job.model_name or "bytedance/seedance-2.0"),
            "state": "draft",
        }
        attempt = self.Attempt.create(vals)
        if state != "draft":
            self.env.cr.execute(
                "UPDATE t2av_attempt SET state = %s WHERE id = %s",
                (state, attempt.id),
            )
            attempt.invalidate_recordset()
        return attempt

    def _make_human_review(self, attempt, state="queued", assigned_to=None):
        review = self.Review.sudo().create({
            "attempt_id": attempt.id,
            "provider": "human",
            "state": "queued",
        })
        updates = []
        params = []
        if state != "queued":
            updates.append("state = %s")
            params.append(state)
        if assigned_to is not None:
            updates.append("assigned_to_id = %s")
            params.append(assigned_to.id)
            updates.append("locked_at = NOW() AT TIME ZONE 'UTC'")
        if updates:
            params.append(review.id)
            self.env.cr.execute(
                f"UPDATE t2av_video_review SET {', '.join(updates)} WHERE id = %s",
                params,
            )
            review.invalidate_recordset()
        return review

    def test_toggle_default_off(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "False")
        job = self._make_job()
        rec = job.with_user(self.manager_user)
        self.assertFalse(rec.gemini_qc_active)

    def test_toggle_on_makes_gemini_active(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "True")
        job = self._make_job()
        rec = job.with_user(self.manager_user)
        self.assertTrue(rec.gemini_qc_active)

    def test_auto_enqueue_on_attempt_done(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "False")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        attempt._on_state_done()
        reviews = self.Review.search([("attempt_id", "=", attempt.id), ("provider", "=", "human")])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews.state, "queued")

    def test_auto_enqueue_skipped_when_toggle_on(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "True")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        attempt._on_state_done()
        reviews = self.Review.search([("attempt_id", "=", attempt.id), ("provider", "=", "human")])
        self.assertFalse(reviews)

    def test_auto_enqueue_idempotent(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "False")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        attempt._on_state_done()
        attempt._on_state_done()
        reviews = self.Review.search([("attempt_id", "=", attempt.id), ("provider", "=", "human")])
        self.assertEqual(len(reviews), 1)

    def test_apply_reviewer_verdict_pass(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "Looks crisp; audio aligned.")
        review.invalidate_recordset()
        self.assertEqual(review.state, "done")
        self.assertEqual(review.verdict, "accept")
        self.assertTrue(review.reviewer_notes)
        self.assertEqual(review.effective_verdict, "accept")

    def test_apply_reviewer_verdict_fail(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        review.with_user(self.reviewer_user)._apply_reviewer_verdict("reject", "Lip-sync off after 3s.")
        review.invalidate_recordset()
        self.assertEqual(review.verdict, "reject")
        self.assertEqual(review.effective_verdict, "reject")

    def test_apply_reviewer_verdict_requires_notes(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        with self.assertRaises(ValidationError):
            review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "")
        with self.assertRaises(ValidationError):
            review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "   \n  ")

    def test_apply_reviewer_verdict_invalid_verdict(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        with self.assertRaises(ValidationError):
            review.with_user(self.reviewer_user)._apply_reviewer_verdict("maybe", "notes")

    def test_apply_reviewer_verdict_wrong_user(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        with self.assertRaises(ValidationError):
            review.with_user(self.reviewer_user_2)._apply_reviewer_verdict("accept", "Looks ok.")

    def test_apply_reviewer_verdict_wrong_state(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="queued")
        with self.assertRaises(ValidationError):
            review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "Looks ok.")

    def test_apply_reviewer_verdict_wrong_provider(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self.Review.sudo().create({
            "attempt_id": attempt.id,
            "provider": "bedrock",
            "state": "queued",
        })
        self.env.cr.execute(
            "UPDATE t2av_video_review SET state = 'done', verdict = 'accept' WHERE id = %s",
            (review.id,),
        )
        review.invalidate_recordset()
        with self.assertRaises(ValidationError):
            review._apply_reviewer_verdict("accept", "Looks ok.")

    def test_manager_cannot_override_human_review(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "Original verdict.")
        review.invalidate_recordset()
        with self.assertRaises(ValidationError):
            review.with_user(self.manager_user)._apply_human_override("reject", "Manager override attempt.")

    def test_admin_can_override_human_review(self):
        admin = self.env.ref("base.user_admin")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        review.with_user(self.reviewer_user)._apply_reviewer_verdict("accept", "Original.")
        review.invalidate_recordset()
        review.with_user(admin)._apply_human_override("reject", "Admin reverses for compliance.")
        review.invalidate_recordset()
        self.assertEqual(review.human_verdict, "reject")
        self.assertEqual(review.effective_verdict, "reject")

    def test_cron_skips_human_reviews(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        human_review = self._make_human_review(attempt, state="queued")
        self.env.cr.execute(
            "SELECT id FROM t2av_video_review WHERE state = 'queued' AND (provider IS NULL OR provider != 'human')"
        )
        ids_for_cron = {row[0] for row in self.env.cr.fetchall()}
        self.assertNotIn(human_review.id, ids_for_cron)

    def test_admin_force_release_lock(self):
        admin = self.env.ref("base.user_admin")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        review.with_user(admin).admin_force_release_lock()
        review.invalidate_recordset()
        self.assertEqual(review.state, "queued")
        self.assertFalse(review.assigned_to_id)
        self.assertFalse(review.locked_at)

    def test_non_admin_cannot_force_release(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="assigned", assigned_to=self.reviewer_user)
        with self.assertRaises(ValidationError):
            review.with_user(self.manager_user).admin_force_release_lock()

    def test_force_release_no_op_when_not_assigned(self):
        admin = self.env.ref("base.user_admin")
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="queued")
        result = review.with_user(admin).admin_force_release_lock()
        self.assertFalse(result)

    def test_wizard_open_next_claims_atomically(self):
        job1 = self._make_job(prompt="cat surf one")
        job2 = self._make_job(prompt="cat surf two")
        a1 = self._spawn_attempt(job1, state="done")
        a2 = self._spawn_attempt(job2, state="done")
        r1 = self._make_human_review(a1, state="queued")
        r2 = self._make_human_review(a2, state="queued")

        action = self.Wizard.with_user(self.reviewer_user).open_next()
        self.assertEqual(action.get("type"), "ir.actions.act_window")
        self.assertEqual(action.get("res_model"), "t2av.stack.review.wizard")
        wizard_id = action.get("res_id")
        wizard = self.Wizard.browse(wizard_id)
        claimed_review = wizard.review_id
        self.assertIn(claimed_review, r1 | r2)
        claimed_review.invalidate_recordset()
        self.assertEqual(claimed_review.state, "assigned")
        self.assertEqual(claimed_review.assigned_to_id, self.reviewer_user)
        self.assertTrue(claimed_review.locked_at)

        action2 = self.Wizard.with_user(self.reviewer_user_2).open_next()
        wizard2 = self.Wizard.browse(action2["res_id"])
        claimed2 = wizard2.review_id
        self.assertNotEqual(claimed2.id, claimed_review.id)
        self.assertEqual(claimed2.assigned_to_id, self.reviewer_user_2)

        action3 = self.Wizard.with_user(self.reviewer_user).open_next()
        self.assertEqual(action3.get("type"), "ir.actions.client")
        self.assertEqual(action3.get("tag"), "display_notification")

    def test_wizard_save_next_applies_verdict_and_chains(self):
        job1 = self._make_job(prompt="cat A")
        job2 = self._make_job(prompt="cat B")
        a1 = self._spawn_attempt(job1, state="done")
        a2 = self._spawn_attempt(job2, state="done")
        self._make_human_review(a1, state="queued")
        self._make_human_review(a2, state="queued")

        WizUser = self.Wizard.with_user(self.reviewer_user)
        action = WizUser.open_next()
        wizard = self.Wizard.browse(action["res_id"]).with_user(self.reviewer_user)
        wizard.write({"qc_status": "pass", "reviewer_notes": "Solid."})
        next_action = wizard.action_save_next()
        wizard.review_id.invalidate_recordset()
        self.assertEqual(wizard.review_id.state, "done")
        self.assertEqual(wizard.review_id.verdict, "accept")
        self.assertEqual(next_action.get("type"), "ir.actions.act_window")

    def test_wizard_cancel_releases_lock(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        self._make_human_review(attempt, state="queued")
        WizUser = self.Wizard.with_user(self.reviewer_user)
        action = WizUser.open_next()
        wizard = self.Wizard.browse(action["res_id"]).with_user(self.reviewer_user)
        review = wizard.review_id
        wizard.action_cancel()
        review.invalidate_recordset()
        self.assertEqual(review.state, "queued")
        self.assertFalse(review.assigned_to_id)
        self.assertFalse(review.locked_at)

    def test_wizard_save_next_requires_notes_via_constraint(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        self._make_human_review(attempt, state="queued")
        WizUser = self.Wizard.with_user(self.reviewer_user)
        action = WizUser.open_next()
        wizard = self.Wizard.browse(action["res_id"]).with_user(self.reviewer_user)
        with self.assertRaises(ValidationError):
            wizard.write({"qc_status": "pass", "reviewer_notes": "   "})

    def test_wizard_requires_reviewer_or_admin_group(self):
        plain_user = self.env["res.users"].create({
            "name": "Plain User",
            "login": "plain_user@t2av.test",
            "email": "plain_user@t2av.test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(UserError):
            self.Wizard.with_user(plain_user).open_next()

    def test_gemini_action_blocked_when_toggle_off(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "False")
        job = self._make_job()
        self._spawn_attempt(job, state="done")
        with self.assertRaises(UserError):
            job.action_run_review()

    def test_gemini_batch_action_blocked_when_toggle_off(self):
        self.ICP.set_param("t2av.enable_gemini_qc", "False")
        job = self._make_job()
        self._spawn_attempt(job, state="done")
        with self.assertRaises(UserError):
            job.action_batch_run_review()

    def test_export_columns_include_enriched_prompt(self):
        from odoo.addons.t2av.wizards.t2av_sequence_sheet_export_wizard import (
            _PASSED_COLUMNS, _PASSED_HEADERS, _FAILED_COLUMNS, _FAILED_HEADERS,
        )
        self.assertIn("enriched_prompt", _PASSED_COLUMNS)
        self.assertIn("enriched_prompt", _PASSED_HEADERS)
        self.assertIn("enriched_prompt", _FAILED_COLUMNS)
        self.assertIn("enriched_prompt", _FAILED_HEADERS)

    def test_partial_index_present(self):
        self.env.cr.execute(
            """SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_t2av_review_stack_queue'"""
        )
        self.assertTrue(self.env.cr.fetchone(), "Stack-queue partial index missing")

    def test_provider_human_in_selection(self):
        provider_values = dict(self.Review._fields["provider"].selection)
        self.assertIn("human", provider_values)

    def test_state_assigned_in_selection(self):
        state_values = dict(self.Review._fields["state"].selection)
        self.assertIn("assigned", state_values)

    def test_legal_state_transition_queued_to_assigned(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="queued")
        review.sudo().write({"state": "assigned", "assigned_to_id": self.reviewer_user.id})
        self.assertEqual(review.state, "assigned")

    def test_illegal_state_transition_queued_to_done(self):
        job = self._make_job()
        attempt = self._spawn_attempt(job, state="done")
        review = self._make_human_review(attempt, state="queued")
        with self.assertRaises(ValidationError):
            review.sudo().write({"state": "done"})
