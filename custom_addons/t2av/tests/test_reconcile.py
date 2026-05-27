"""T2AV v1.1 reconcile flow tests.

``job.action_reconcile()`` now simply delegates to ``attempt._run_poll()``
synchronously. These tests cover the guard, the delegation, and the
orphaned-attempt fallback inside ``_run_poll``.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "t2av")
class TestReconcile(TransactionCase):

    def _make_job_with_attempt(self, attempt_state="processing", job_id="test_job_123"):
        job = self.env["t2av.generation"].create({
            "prompt": "test prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        })
        attempt = self.env["t2av.attempt"].create({
            "job_id": job.id,
            "attempt_number": 1,
            "prompt": job.prompt,
            "duration": job.duration,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "seed": job.seed or 0,
            "generate_audio": job.generate_audio,
            "state": "draft",
        })
        # Bypass state-machine guard via SQL for setup
        self.env.cr.execute(
            "UPDATE t2av_attempt SET state=%s, openrouter_job_id=%s WHERE id=%s",
            (attempt_state, job_id, attempt.id),
        )
        attempt.invalidate_recordset()
        job.invalidate_recordset()
        return job, attempt

    def test_reconcile_state_guard(self):
        """Reconcile raises UserError when no in-flight attempt exists."""
        job, _ = self._make_job_with_attempt(attempt_state="done")
        with self.assertRaises(UserError):
            job.action_reconcile()

    def test_reconcile_calls_run_poll(self):
        """Reconcile invokes _run_poll on the active attempt."""
        job, attempt = self._make_job_with_attempt(attempt_state="processing")
        with patch.object(type(attempt), "_run_poll") as mock_poll:
            job.action_reconcile()
            mock_poll.assert_called_once()

    def test_reconcile_no_job_id_orphaned(self):
        """An attempt with no openrouter_job_id calls _run_poll which itself
        marks the attempt failed with error_code=orphaned."""
        job = self.env["t2av.generation"].create({
            "prompt": "test",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
        })
        attempt = self.env["t2av.attempt"].create({
            "job_id": job.id,
            "attempt_number": 1,
            "prompt": "test",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "state": "draft",
        })
        # Set state=processing via SQL but leave openrouter_job_id empty
        self.env.cr.execute(
            "UPDATE t2av_attempt SET state='processing' WHERE id=%s",
            (attempt.id,),
        )
        attempt.invalidate_recordset()
        job.invalidate_recordset()
        attempt._run_poll()
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(attempt.error_code, "orphaned")
