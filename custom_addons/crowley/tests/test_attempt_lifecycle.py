"""Crowley v1.1 attempt-lifecycle integration tests.

Most behavior is covered in ``test_state_machine.py``. Reserved for future
end-to-end mocked tests (full pipeline with mocked OpenRouter + S3).
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "crowley")
class TestAttemptLifecycle(TransactionCase):

    def test_module_loads(self):
        """Sanity: crowley.attempt model exists with expected fields."""
        Attempt = self.env["crowley.attempt"]
        for f in ("job_id", "attempt_number", "state", "prompt",
                  "change_log", "cost_usd", "video_s3_url"):
            self.assertIn(f, Attempt._fields, f"Attempt missing field: {f}")
