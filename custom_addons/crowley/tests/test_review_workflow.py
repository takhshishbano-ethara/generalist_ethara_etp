"""Crowley v1.3 review workflow tests.

Covers the manager approve/reject workflow on video attempts:
- Auto-set review_state=pending when attempt reaches done
- Manager approve and reject via wizard
- Access control (user vs manager)
- Guard checks (state, review_state)
- Chatter messages
- Job-level review_state proxy
- Independent per-attempt reviews
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "crowley")
class TestReviewWorkflow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager_group = cls.env.ref("crowley.group_crowley_manager")
        cls.user_group = cls.env.ref("crowley.group_crowley_user")

        cls.manager_user = cls.env["res.users"].create({
            "name": "Test Manager",
            "login": "crowley_test_manager",
            "groups_id": [(4, cls.manager_group.id)],
        })
        cls.regular_user = cls.env["res.users"].create({
            "name": "Test User",
            "login": "crowley_test_user",
            "groups_id": [(4, cls.user_group.id)],
        })

    def _make_job(self, **vals):
        defaults = {
            "prompt": "A cat surfing in slow motion",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "category": "human_activities",
        }
        defaults.update(vals)
        return self.env["crowley.generation"].create(defaults)

    def _spawn_attempt(self, job, attempt_number, state="draft", **overrides):
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
        attempt = self.env["crowley.attempt"].create(vals)
        if state != "draft":
            update_parts = ["state = %s"]
            update_vals = [state]
            if state == "done":
                update_parts.append("review_state = 'pending'")
            update_vals.append(attempt.id)
            self.env.cr.execute(
                f"UPDATE crowley_attempt SET {', '.join(update_parts)} WHERE id = %s",
                update_vals,
            )
            attempt.invalidate_recordset()
        return attempt

    # ------------------------------------------------------------------
    # Auto-set review_state on done
    # ------------------------------------------------------------------
    def test_done_attempt_gets_pending_review(self):
        """When an attempt transitions to done via write(), review_state is auto-set to pending."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="downloading")
        attempt.write({"state": "done"})
        self.assertEqual(attempt.review_state, "pending")

    def test_non_done_has_no_review_state(self):
        """Attempts not in done state should not have a review_state."""
        job = self._make_job()
        for state in ("draft", "queued", "failed"):
            attempt = self._spawn_attempt(job, 1, state=state)
            self.assertFalse(attempt.review_state)
            attempt.unlink()

    # ------------------------------------------------------------------
    # Manager approve
    # ------------------------------------------------------------------
    def test_manager_approve(self):
        """A manager can approve a done+pending attempt via the wizard."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")
        self.assertEqual(attempt.review_state, "pending")

        wizard = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": attempt.id,
            "review_action": "approve",
            "review_reason": "Looks great",
        })
        wizard.action_confirm()

        attempt.invalidate_recordset()
        self.assertEqual(attempt.review_state, "approved")
        self.assertEqual(attempt.reviewed_by, self.manager_user)
        self.assertTrue(attempt.reviewed_at)
        self.assertEqual(attempt.review_reason, "Looks great")

    # ------------------------------------------------------------------
    # Manager reject
    # ------------------------------------------------------------------
    def test_manager_reject_with_reason(self):
        """A manager can reject a done+pending attempt via the wizard with a reason."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")

        wizard = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": attempt.id,
            "review_action": "reject",
            "review_reason": "Video quality too low",
        })
        wizard.action_confirm()

        attempt.invalidate_recordset()
        self.assertEqual(attempt.review_state, "rejected")
        self.assertEqual(attempt.reviewed_by, self.manager_user)
        self.assertTrue(attempt.reviewed_at)
        self.assertEqual(attempt.review_reason, "Video quality too low")

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------
    def test_user_cannot_approve(self):
        """A regular user calling action_approve gets UserError."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")
        with self.assertRaises(UserError):
            attempt.with_user(self.regular_user).action_approve()

    def test_user_cannot_reject(self):
        """A regular user calling action_reject gets UserError."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")
        with self.assertRaises(UserError):
            attempt.with_user(self.regular_user).action_reject()

    # ------------------------------------------------------------------
    # Guard checks
    # ------------------------------------------------------------------
    def test_cannot_review_non_done(self):
        """Attempting to approve a non-done attempt raises UserError."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="processing")
        with self.assertRaises(UserError):
            attempt.with_user(self.manager_user).action_approve()

    def test_cannot_re_review(self):
        """Attempting to approve an already-approved attempt raises UserError."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")

        wizard = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": attempt.id,
            "review_action": "approve",
        })
        wizard.action_confirm()

        with self.assertRaises(UserError):
            attempt.with_user(self.manager_user).action_approve()

    def test_reject_requires_reason(self):
        """The wizard rejects a rejection without a reason."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")

        with self.assertRaises(ValidationError):
            self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
                "attempt_id": attempt.id,
                "review_action": "reject",
                "review_reason": "",
            })

    def test_user_cannot_write_review_fields(self):
        """A regular user cannot directly write review fields on an attempt."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")

        with self.assertRaises(UserError):
            attempt.with_user(self.regular_user).write({"review_state": "approved"})

    # ------------------------------------------------------------------
    # Chatter
    # ------------------------------------------------------------------
    def test_review_posts_chatter(self):
        """Approval posts a message on the job's chatter."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")
        msg_count_before = len(job.message_ids)

        wizard = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": attempt.id,
            "review_action": "approve",
            "review_reason": "Perfect",
        })
        wizard.action_confirm()

        self.assertGreater(len(job.message_ids), msg_count_before)

    # ------------------------------------------------------------------
    # Job-level proxy
    # ------------------------------------------------------------------
    def test_job_review_state_proxy(self):
        """The job's review_state mirrors the active attempt's review_state."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, 1, state="done")
        job.invalidate_recordset()
        self.assertEqual(job.review_state, "pending")

        wizard = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": attempt.id,
            "review_action": "approve",
        })
        wizard.action_confirm()

        job.invalidate_recordset()
        self.assertEqual(job.review_state, "approved")

    # ------------------------------------------------------------------
    # Independent per-attempt
    # ------------------------------------------------------------------
    def test_independent_per_attempt(self):
        """Each attempt can be reviewed independently."""
        job = self._make_job()
        a1 = self._spawn_attempt(job, 1, state="done")
        a2 = self._spawn_attempt(job, 2, state="done")

        wizard1 = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": a1.id,
            "review_action": "approve",
        })
        wizard1.action_confirm()

        wizard2 = self.env["crowley.attempt.review.wizard"].with_user(self.manager_user).create({
            "attempt_id": a2.id,
            "review_action": "reject",
            "review_reason": "Audio out of sync",
        })
        wizard2.action_confirm()

        a1.invalidate_recordset()
        a2.invalidate_recordset()
        self.assertEqual(a1.review_state, "approved")
        self.assertEqual(a2.review_state, "rejected")

    # ------------------------------------------------------------------
    # Original prompt field
    # ------------------------------------------------------------------
    def test_original_prompt_field_exists(self):
        """original_prompt is writable on crowley.generation."""
        job = self._make_job(original_prompt="My raw idea for a video")
        self.assertEqual(job.original_prompt, "My raw idea for a video")

    def test_original_prompt_independent_of_prompt(self):
        """Changing prompt does not affect original_prompt and vice versa."""
        job = self._make_job(
            original_prompt="Original idea",
            prompt="Enriched version of the idea",
        )
        self.assertEqual(job.original_prompt, "Original idea")
        self.assertEqual(job.prompt, "Enriched version of the idea")

        job.write({"prompt": "New enriched prompt"})
        self.assertEqual(job.original_prompt, "Original idea")

        job.write({"original_prompt": "Updated original"})
        self.assertEqual(job.prompt, "New enriched prompt")
