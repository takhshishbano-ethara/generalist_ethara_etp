"""T2AV v1.1 Job + Attempt state-machine tests.

Most fields moved off the parent ``t2av.generation`` (job) onto the new
``t2av.attempt`` rows. These tests cover the new schema:

- Job defaults / sequence
- Attempt creation, uniqueness, range constraints
- Job-level guards: max 3 attempts, unlink guard, retry diff requirement
- Change log diff vs prior attempt
- Cost roll-up across attempts
- Attempt-level state-machine transitions
- Prompt validation
"""
from unittest.mock import patch

import psycopg2

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "t2av")
class TestStateMachine(TransactionCase):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_job(self, **vals):
        defaults = {
            "prompt": "A cat surfing in slow motion",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        }
        defaults.update(vals)
        return self.env["t2av.generation"].create(defaults)

    def _spawn_attempt(self, job, attempt_number, state="draft", **overrides):
        """Create an attempt under the job, bypassing the state-machine guard for setup.

        For non-draft states, the state-machine guard would reject the direct write,
        so we use SQL UPDATE after creation.
        """
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
        cost_usd = overrides.pop("cost_usd", None)
        attempt = self.env["t2av.attempt"].create(vals)
        if state != "draft" or cost_usd is not None:
            update_parts = []
            update_vals = []
            if state != "draft":
                update_parts.append("state = %s")
                update_vals.append(state)
            if cost_usd is not None:
                update_parts.append("cost_usd = %s")
                update_vals.append(cost_usd)
            update_vals.append(attempt.id)
            self.env.cr.execute(
                f"UPDATE t2av_attempt SET {', '.join(update_parts)} WHERE id = %s",
                update_vals,
            )
            attempt.invalidate_recordset()
        return attempt

    # ------------------------------------------------------------------
    # Defaults / sequence
    # ------------------------------------------------------------------
    def test_default_name_uses_sequence(self):
        """Job's name should follow the CRW000000 sequence."""
        job = self._make_job()
        self.assertRegex(job.name, r"^CRW\d{6}$")

    def test_default_state_is_draft(self):
        """A newly-created job with no attempts is in draft state."""
        job = self._make_job()
        self.assertEqual(job.state, "draft")
        self.assertEqual(job.attempts_used, 0)
        self.assertEqual(job.attempts_remaining, 3)

    # ------------------------------------------------------------------
    # Attempt creation / numbering / uniqueness
    # ------------------------------------------------------------------
    def test_attempt_number_starts_at_one(self):
        """First attempt on a job has attempt_number=1."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, attempt_number=1, state="draft")
        self.assertEqual(attempt.attempt_number, 1)

    def test_attempt_number_unique_per_job(self):
        """SQL constraint: cannot have two attempts with the same number on one job."""
        job = self._make_job()
        self._spawn_attempt(job, attempt_number=1, state="draft")
        # Force flush so the second create hits the SQL constraint inside the savepoint.
        raised = False
        try:
            with self.env.cr.savepoint():
                self._spawn_attempt(job, attempt_number=1, state="draft")
                self.env.flush_all()
        except (psycopg2.errors.UniqueViolation, Exception):
            raised = True
        self.assertTrue(raised, "Expected uniqueness violation on duplicate attempt_number")

    def test_attempt_number_range(self):
        """attempt_number must be 1-3."""
        job = self._make_job()
        for bad in (0, 4, 7, -1):
            raised = False
            try:
                with self.env.cr.savepoint():
                    self._spawn_attempt(job, attempt_number=bad, state="draft")
            except (psycopg2.errors.CheckViolation, ValidationError):
                raised = True
            self.assertTrue(raised, f"Expected check violation on attempt_number={bad}")

    def test_max_three_attempts_enforced(self):
        """Spawning a 4th attempt raises UserError."""
        job = self._make_job()
        for n in (1, 2, 3):
            self._spawn_attempt(job, attempt_number=n, state="done")
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job._spawn_attempt()

    # ------------------------------------------------------------------
    # Attempts label
    # ------------------------------------------------------------------
    def test_attempts_label(self):
        """The label string flips correctly."""
        job = self._make_job()
        self.assertIn("No attempts yet", job.attempts_label)
        self._spawn_attempt(job, attempt_number=1, state="done")
        job.invalidate_recordset()
        self.assertIn("1/3", job.attempts_label)
        self._spawn_attempt(job, attempt_number=2, state="done")
        self._spawn_attempt(job, attempt_number=3, state="done")
        job.invalidate_recordset()
        self.assertIn("3/3", job.attempts_label)
        self.assertIn("max reached", job.attempts_label.lower())

    # ------------------------------------------------------------------
    # Change log
    # ------------------------------------------------------------------
    def test_first_attempt_changelog_is_initial(self):
        """Attempt #1's change_log says 'Initial attempt'."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, attempt_number=1, state="done", prompt="cat surf")
        # change_log is computed on write/read — force recompute
        attempt.invalidate_recordset()
        self.assertEqual(attempt.change_log, "Initial attempt")

    def test_diff_changelog_records_field_changes(self):
        """Attempt N's change_log shows the diff vs attempt N-1."""
        job = self._make_job(duration="5", resolution="720p", aspect_ratio="16:9")
        self._spawn_attempt(job, attempt_number=1, state="done",
                            prompt="cat surf", duration="5", resolution="720p", aspect_ratio="16:9")
        a2 = self._spawn_attempt(job, attempt_number=2, state="done",
                                 prompt="cat surf", duration="10", resolution="720p", aspect_ratio="16:9")
        a2.invalidate_recordset()
        self.assertIn("duration: 5", a2.change_log)
        self.assertIn("10", a2.change_log)

    # ------------------------------------------------------------------
    # Retry flow
    # ------------------------------------------------------------------
    def test_retry_requires_field_change(self):
        """action_submit_retry without any input change raises UserError."""
        job = self._make_job(prompt="initial prompt", duration="5")
        self._spawn_attempt(
            job, attempt_number=1, state="done",
            prompt="initial prompt", duration="5",
            resolution=job.resolution, aspect_ratio=job.aspect_ratio,
            seed=job.seed, generate_audio=job.generate_audio,
        )
        job.invalidate_recordset()
        job.write({"ui_retry_pending": True})
        with self.assertRaises(UserError) as ctx:
            job.action_submit_retry()
        self.assertIn("change at least one field", str(ctx.exception))

    def test_retry_with_changed_prompt_spawns_attempt2(self):
        """When the prompt changes, action_submit_retry spawns attempt 2 and defers submit."""
        job = self._make_job(prompt="initial prompt")
        self._spawn_attempt(
            job, attempt_number=1, state="done",
            prompt="initial prompt", duration=job.duration,
            resolution=job.resolution, aspect_ratio=job.aspect_ratio,
            seed=job.seed, generate_audio=job.generate_audio,
        )
        job.invalidate_recordset()
        job.write({"ui_retry_pending": True, "prompt": "different prompt"})
        # Stub out _defer so we don't actually call OpenRouter
        with patch.object(type(job.env["t2av.attempt"]), "_defer", return_value=None):
            # Also stub _validate_can_submit (which needs API key + S3 connector configured)
            with patch.object(type(job), "_validate_can_submit", return_value=None):
                job.action_submit_retry()
        self.assertEqual(job.attempts_used, 2)
        a2 = job.attempt_ids.sorted("attempt_number", reverse=True)[:1]
        self.assertEqual(a2.attempt_number, 2)
        self.assertEqual(a2.prompt, "different prompt")
        self.assertFalse(job.ui_retry_pending)

    # ------------------------------------------------------------------
    # Cost roll-up
    # ------------------------------------------------------------------
    def test_total_cost_sums_attempt_costs(self):
        """Job's total_cost_usd is the sum of all attempt costs."""
        job = self._make_job()
        self._spawn_attempt(job, attempt_number=1, state="done", cost_usd=0.5)
        self._spawn_attempt(job, attempt_number=2, state="done", cost_usd=0.75)
        self._spawn_attempt(job, attempt_number=3, state="done", cost_usd=0.25)
        job.invalidate_recordset()
        self.assertAlmostEqual(job.total_cost_usd, 1.5, places=4)
        self.assertAlmostEqual(job.cost_usd, 1.5, places=4)

    # ------------------------------------------------------------------
    # Unlink guard
    # ------------------------------------------------------------------
    def test_cannot_unlink_job_with_in_flight_attempt(self):
        """unlink raises UserError if any attempt is in a non-terminal state."""
        job = self._make_job()
        self._spawn_attempt(job, attempt_number=1, state="processing")
        job.invalidate_recordset()
        with self.assertRaises(UserError):
            job.unlink()

    # ------------------------------------------------------------------
    # Attempt-level state-machine transitions
    # ------------------------------------------------------------------
    def test_attempt_illegal_transition_raises(self):
        """Cannot transition attempt from draft to done directly."""
        job = self._make_job()
        attempt = self._spawn_attempt(job, attempt_number=1, state="draft")
        with self.assertRaises(ValidationError):
            attempt.write({"state": "done"})

    # ------------------------------------------------------------------
    # Prompt validation
    # ------------------------------------------------------------------
    def test_prompt_validation(self):
        """Empty / whitespace / too-long prompts are rejected."""
        job = self._make_job()
        with self.assertRaises(ValidationError):
            job.write({"prompt": ""})
        with self.assertRaises(ValidationError):
            job.write({"prompt": "   "})
        with self.assertRaises(ValidationError):
            job.write({"prompt": "x" * 2001})
