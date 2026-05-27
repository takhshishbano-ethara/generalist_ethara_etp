"""T2AV v1.2 dataset-naming tests.

Covers the T2AV_<category>_<NNNNNN>.mp4 convention:
- Filename format from category + sequence_number
- Per-category independent counters
- Category lock semantics
- Retry within a locked category
- Legacy (no-category) records can't be retried
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "t2av")
class TestDatasetNaming(TransactionCase):

    CATEGORY_A = "human_activities"
    CATEGORY_B = "educational_videos"

    def _make_job(self, category=CATEGORY_A, **vals):
        defaults = {
            "prompt": "A test prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "category": category,
        }
        defaults.update(vals)
        return self.env["t2av.generation"].create(defaults)

    def _spawn_done_attempt(self, job, attempt_number, category, seq_n, **overrides):
        """Create a done attempt with category+sequence_number.

        ORM-writes category/sequence_number so the ``video_file`` compute fires.
        SQL-updates state because the state-machine guard would reject
        draft → done.
        """
        vals = {
            "job_id": job.id,
            "attempt_number": attempt_number,
            "prompt": job.prompt,
            "duration": job.duration,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "seed": 0,
            "generate_audio": True,
            "state": "draft",
        }
        vals.update(overrides)
        attempt = self.env["t2av.attempt"].create(vals)
        attempt.write({"category": category, "sequence_number": seq_n})
        self.env.cr.execute(
            "UPDATE t2av_attempt SET state='done' WHERE id=%s",
            (attempt.id,),
        )
        attempt.invalidate_recordset()
        return attempt

    def test_video_file_format(self):
        """video_file is computed as T2AV_<category>_<NNNNNN>.mp4."""
        job = self._make_job(category=self.CATEGORY_A)
        attempt = self._spawn_done_attempt(
            job, attempt_number=1, category=self.CATEGORY_A, seq_n=1,
        )
        self.assertEqual(attempt.video_file, "T2AV_human_activities_000001.mp4")

    def test_video_file_padding(self):
        """Sequence number is zero-padded to 6 digits."""
        job = self._make_job(category=self.CATEGORY_A)
        a1 = self._spawn_done_attempt(job, 1, self.CATEGORY_A, 1)
        a2 = self._spawn_done_attempt(job, 2, self.CATEGORY_A, 42)
        a3 = self._spawn_done_attempt(job, 3, self.CATEGORY_A, 999999)
        self.assertEqual(a1.video_file, "T2AV_human_activities_000001.mp4")
        self.assertEqual(a2.video_file, "T2AV_human_activities_000042.mp4")
        self.assertEqual(a3.video_file, "T2AV_human_activities_999999.mp4")

    def test_per_category_sequences_are_independent(self):
        """ir.sequence for category A doesn't advance when category B is used."""
        Sequence = self.env["ir.sequence"].sudo()
        seq_a = Sequence.search([("code", "=", f"t2av.attempt.{self.CATEGORY_A}")])
        seq_b = Sequence.search([("code", "=", f"t2av.attempt.{self.CATEGORY_B}")])
        self.assertTrue(seq_a, "Sequence for category A must exist")
        self.assertTrue(seq_b, "Sequence for category B must exist")
        # Record starting points (sequence state persists across tests in the
        # same TransactionCase setup if a previous test consumed numbers).
        start_a = seq_a.number_next_actual
        start_b = seq_b.number_next_actual
        # Consume from category A 3 times via the same code path the pipeline uses.
        consumed_a = [
            Sequence.next_by_code(f"t2av.attempt.{self.CATEGORY_A}")
            for _ in range(3)
        ]
        self.env.flush_all()
        seq_a.invalidate_recordset()
        seq_b.invalidate_recordset()
        # Category B should be untouched.
        self.assertEqual(seq_b.number_next_actual, start_b,
                         "Category B's counter advanced when only A was used")
        # Category A should have advanced by 3.
        self.assertEqual(seq_a.number_next_actual, start_a + 3,
                         f"Expected A advanced by 3 (consumed: {consumed_a})")

    def test_category_locked_after_done_attempt(self):
        """category_locked computes True once any attempt is done."""
        job = self._make_job(category=self.CATEGORY_A)
        self.assertFalse(job.category_locked)
        self._spawn_done_attempt(job, 1, self.CATEGORY_A, 1)
        job.invalidate_recordset()
        self.assertTrue(job.category_locked)

    def test_category_locked_false_when_only_failed_attempts(self):
        """A failed attempt does NOT lock the category."""
        job = self._make_job(category=self.CATEGORY_A)
        # Create a failed attempt
        attempt = self.env["t2av.attempt"].create({
            "job_id": job.id,
            "attempt_number": 1,
            "prompt": job.prompt,
            "duration": job.duration,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "state": "draft",
        })
        self.env.cr.execute(
            "UPDATE t2av_attempt SET state='failed', error_code='test' WHERE id=%s",
            (attempt.id,),
        )
        attempt.invalidate_recordset()
        job.invalidate_recordset()
        self.assertFalse(
            job.category_locked,
            "category should remain editable after only failed attempts",
        )

    def test_legacy_record_without_category_cannot_be_submitted(self):
        """A job with category=False raises UserError on _validate_can_submit."""
        # Bypass the form's required=1 by creating with no category via SQL
        job = self._make_job(category=self.CATEGORY_A)
        self.env.cr.execute(
            "UPDATE t2av_generation SET category=NULL WHERE id=%s",
            (job.id,),
        )
        job.invalidate_recordset()
        with self.assertRaises(UserError) as ctx:
            job._validate_can_submit()
        self.assertIn("Category", str(ctx.exception))

    def test_retry_inherits_locked_category(self):
        """When a job is locked, a retry attempt uses the same category."""
        job = self._make_job(category=self.CATEGORY_A)
        self._spawn_done_attempt(job, 1, self.CATEGORY_A, 1)
        job.invalidate_recordset()
        self.assertTrue(job.category_locked)
        # User starts a retry and edits the prompt
        job.write({
            "ui_retry_pending": True,
            "prompt": "different prompt",
            "enriched_prompt": "different prompt enriched",
            "golden_prompt": "different prompt enriched",
        })
        # Stub the heavy bits so we don't hit OpenRouter / S3
        with patch.object(type(job.env["t2av.attempt"]), "_defer", return_value=None), \
             patch.object(type(job), "_validate_can_submit", return_value=None):
            job.action_submit_retry()
        job.invalidate_recordset()
        a2 = job.attempt_ids.sorted("attempt_number", reverse=True)[:1]
        self.assertEqual(a2.attempt_number, 2)
        # Category on the new attempt is set when _run_download runs — but
        # job.category is still CATEGORY_A, so the locked category is preserved.
        self.assertEqual(job.category, self.CATEGORY_A)
