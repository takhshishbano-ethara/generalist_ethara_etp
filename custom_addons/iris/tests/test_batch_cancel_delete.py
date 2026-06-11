"""Batch cancellation + deletion semantics (v1.1).

* ``action_cancel``: draft/screening only; member screenings already in
  flight keep running on the individual candidates.
* ``unlink``: blocked while the batch is in flight (screening/consistency);
  allowed from draft/cancelled/failed/done; the report attachment dies with
  the batch.
* Below-viability auto-cancel: a non-draft batch that drops under 2 members
  (member hard-delete or detach) is cancelled by the settlement choke point
  / cron safety sweep, with a chatter note.
"""

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import DEFAULT_LLM_RESULT, IrisCase, mock_llm


def _result(content):
    return dict(DEFAULT_LLM_RESULT, content=content)


@tagged("post_install", "-at_install", "iris")
class TestBatchCancelDelete(IrisCase):
    def _done_batch(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        with mock_llm(self.UNPARSEABLE_BATCH_REPORT):
            self._run_llm_queue()
        self.assertEqual(batch.state, "done")
        return batch

    def _failed_batch(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        with mock_llm(side_effect=Exception("boom")):
            self._run_llm_queue()
        self.assertEqual(batch.state, "failed")
        return batch

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    def test_cancel_from_draft(self):
        batch = self._make_batch()
        batch.action_cancel()
        self.assertEqual(batch.state, "cancelled")

    def test_cancel_from_screening_leaves_member_screenings_running(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        batch.action_cancel()
        self.assertEqual(batch.state, "cancelled")
        self.assertTrue(any(
            "Batch cancelled" in body for body in self._chatter_bodies(batch)
        ))

        # In-flight member screenings keep going on the candidates...
        self.assertEqual(
            set(batch.screening_ids.mapped("llm_status")), {"queued"},
        )
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(
            set(batch.candidate_ids.mapped("state")), {"shipped"},
        )
        # ...but a cancelled batch never flips to consistency.
        self.assertEqual(batch.state, "cancelled")
        self.assertEqual(batch.llm_status, "none")

    def test_cancel_blocked_from_terminal_states(self):
        done = self._done_batch()
        with self.assertRaises(UserError):
            done.action_cancel()

        failed = self._failed_batch()
        with self.assertRaises(UserError):
            failed.action_cancel()

    def test_cancel_blocked_during_consistency(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        with self.assertRaises(UserError):
            batch.action_cancel()

    # ------------------------------------------------------------------
    # Unlink
    # ------------------------------------------------------------------
    def test_unlink_blocked_while_in_flight(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with self.assertRaises(UserError):
            batch.unlink()

        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        with self.assertRaises(UserError):
            batch.unlink()

    def test_unlink_allowed_from_resting_states(self):
        draft = self._make_batch(candidates=self.env["iris.candidate"])
        draft.unlink()
        self.assertFalse(draft.exists())

        cancelled = self._make_batch(candidates=self.env["iris.candidate"])
        cancelled.write({"state": "cancelled"})
        cancelled.unlink()
        self.assertFalse(cancelled.exists())

        failed = self._failed_batch()
        failed.unlink()
        self.assertFalse(failed.exists())

        done = self._done_batch()
        done.unlink()
        self.assertFalse(done.exists())

    def test_unlink_detaches_members_and_keeps_candidates(self):
        batch = self._make_batch()
        members = batch.candidate_ids
        batch.unlink()
        self.assertTrue(all(member.exists() for member in members))
        self.assertFalse(members.mapped("batch_id"))

    def test_unlink_removes_report_attachment(self):
        batch = self._done_batch()
        attachment = batch.report_attachment_id
        self.assertTrue(attachment.exists())
        batch.unlink()
        self.assertFalse(attachment.exists(),
                         "the report attachment must die with the batch")

    # ------------------------------------------------------------------
    # Below-viability auto-cancel
    # ------------------------------------------------------------------
    def test_member_unlink_below_two_auto_cancels(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        batch.candidate_ids[0].unlink()
        self.assertEqual(len(batch.candidate_ids), 1)

        batch._check_members_settled()
        self.assertEqual(batch.state, "cancelled")
        self.assertTrue(any(
            "fewer than 2 members" in body
            for body in self._chatter_bodies(batch)
        ))

    def test_member_detach_below_two_auto_cancels_via_cron(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        detached = batch.candidate_ids[0]
        detached.write({"batch_id": False})

        # The queue cron processes the remaining screenings; the write-hook
        # feeder (or the tail sweep) then finds the batch under viability.
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(batch.state, "cancelled")

        # The detached candidate's own screening still completed normally.
        self.assertEqual(detached.state, "shipped")
        self.assertFalse(detached.batch_id)

    def test_draft_batch_member_detach_never_cancels(self):
        batch = self._make_batch()
        batch.candidate_ids[0].write({"batch_id": False})
        batch._check_members_settled()
        self.assertEqual(batch.state, "draft",
                         "the choke point only watches screening batches")
