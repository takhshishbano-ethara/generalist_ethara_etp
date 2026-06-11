"""Batch completion detection (v1.1): what blocks, what settles, who flips.

``BATCH_SETTLED_STATES = (shipped, blocked, hold, pending_block)`` is the
batch↔governance contract: HOLD and pending_block members COUNT as settled;
``needs_review`` / ``screening`` / ``draft`` (failed-revert) hold the
consistency pass back. The flip ``screening → consistency`` happens in ONE
idempotent choke point fed by (a) the candidate write-hook and (b) the
queue cron's tail safety sweep; with ``auto_run_consistency=False`` the
batch waits with an activity for the manual button.
"""

from odoo.tests.common import tagged

from .common import DEFAULT_LLM_RESULT, IrisCase, mock_llm
from odoo.addons.iris.models.iris_screening_batch import BATCH_SETTLED_STATES


def _result(content):
    return dict(DEFAULT_LLM_RESULT, content=content)


@tagged("post_install", "-at_install", "iris")
class TestBatchBlocking(IrisCase):
    def _batch_activities(self, batch):
        return self.env["mail.activity"].search([
            ("res_model", "=", "iris.screening.batch"),
            ("res_id", "=", batch.id),
        ])

    # ------------------------------------------------------------------
    # The contract constant itself
    # ------------------------------------------------------------------
    def test_settled_states_contract(self):
        self.assertEqual(
            set(BATCH_SETTLED_STATES),
            {"shipped", "blocked", "hold", "pending_block"},
        )

    # ------------------------------------------------------------------
    # needs_review blocks; the manual verdict releases
    # ------------------------------------------------------------------
    def test_needs_review_blocks_until_manual_verdict(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(side_effect=[
            _result(self.VALID_SHIP_RECORD),
            _result(self.UNPARSEABLE_RECORD),
        ]):
            self._run_llm_queue()

        stuck = batch.candidate_ids.filtered(lambda c: c.state == "needs_review")
        self.assertEqual(len(stuck), 1)
        self.assertEqual(batch.state, "screening",
                         "a needs_review member must hold the batch back")
        self.assertEqual(batch.settled_count, 1)
        self.assertEqual(batch.blocking_candidate_ids, stuck)
        self.assertEqual(batch.llm_status, "none",
                         "the consistency pass must not be enqueued yet")

        # The manager's manual verdict settles the member → the write-hook
        # feeder flips the batch.
        stuck.with_user(self.user_manager).action_manual_verdict_hold()
        self.assertEqual(stuck.state, "hold")
        self.assertEqual(batch.settled_count, 2)
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")

    # ------------------------------------------------------------------
    # Failed screening (draft revert) blocks; retry releases
    # ------------------------------------------------------------------
    def test_failed_screening_blocks_until_retry(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(side_effect=[
            Exception("boom"),
            _result(self.VALID_SHIP_RECORD),
        ]):
            self._run_llm_queue()

        failed = batch.screening_ids.filtered(lambda s: s.llm_status == "failed")
        self.assertEqual(len(failed), 1)
        # The failure reverted its candidate to draft — NOT settled.
        self.assertEqual(failed.candidate_id.state, "draft")
        self.assertEqual(batch.state, "screening")
        self.assertEqual(batch.blocking_candidate_ids, failed.candidate_id)

        failed.action_retry_llm()
        self.assertEqual(failed.candidate_id.state, "screening")
        with mock_llm(self.VALID_HOLD_RECORD):
            self._run_llm_queue()

        # The retried member settled as HOLD — which counts as settled.
        self.assertEqual(failed.candidate_id.state, "hold")
        self.assertEqual(batch.state, "consistency")

    # ------------------------------------------------------------------
    # HOLD + pending_block both count as settled
    # ------------------------------------------------------------------
    def test_hold_and_pending_block_count_settled(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(side_effect=[
            _result(self.VALID_HOLD_RECORD),
            _result(self.VALID_BLOCK_RECORD),
        ]):
            self._run_llm_queue()

        states = set(batch.candidate_ids.mapped("state"))
        self.assertEqual(states, {"hold", "pending_block"})
        self.assertEqual(batch.settled_count, 2)
        self.assertFalse(batch.blocking_candidate_ids)
        self.assertEqual(batch.state, "consistency",
                         "HOLD and pending_block must both settle the batch")

    # ------------------------------------------------------------------
    # Manual mode: activity + button instead of the auto-flip
    # ------------------------------------------------------------------
    def test_manual_mode_raises_activity_then_button_runs(self):
        batch = self._make_batch(auto_run_consistency=False)
        batch.action_screen_batch()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()

        self.assertEqual(batch.settled_count, 2)
        self.assertEqual(batch.state, "screening",
                         "manual mode must NOT auto-flip to consistency")
        self.assertEqual(batch.llm_status, "none")

        summary = f"Run consistency review — {batch.name}"
        activities = self._batch_activities(batch)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.summary, summary)
        self.assertEqual(activities.user_id, batch.user_id)
        self.assertTrue(any(
            "Auto-run is disabled" in body
            for body in self._chatter_bodies(batch)
        ))

        # Repeated feeder calls never duplicate the activity.
        batch._check_members_settled()
        batch._check_members_settled()
        self.assertEqual(len(self._batch_activities(batch)), 1)

        # The manual button performs the same guarded transition.
        batch.action_run_consistency()
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")
        self.assertFalse(self._batch_activities(batch),
                         "starting the review must complete the activity")

        report = self.VALID_BATCH_REPORT.format(
            ref1=batch.candidate_ids.sorted("id")[0].reference,
            ref2=batch.candidate_ids.sorted("id")[1].reference,
        )
        with mock_llm(report):
            self._run_llm_queue()
        self.assertEqual(batch.state, "done")

    # ------------------------------------------------------------------
    # Cron tail safety sweep (write-hook feeder missed)
    # ------------------------------------------------------------------
    def test_cron_safety_net_flips_without_write_hook(self):
        batch = self._make_batch()
        # Settle the members while the batch is still DRAFT: the write-hook
        # feeder no-ops (it only watches batches already in screening) —
        # simulating a missed hook.
        batch.candidate_ids[0].write({"state": "shipped"})
        batch.candidate_ids[1].write({"state": "hold"})
        batch.write({"state": "screening"})
        self.assertEqual(batch.state, "screening")
        self.assertEqual(batch.settled_count, 2)

        # The queue cron's tail sweep picks the batch up.
        self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")

    def test_write_hook_ignores_non_screening_batches(self):
        batch = self._make_batch()
        # Member settles while the batch is draft → no flip.
        batch.candidate_ids[0].write({"state": "shipped"})
        batch.candidate_ids[1].write({"state": "shipped"})
        self.assertEqual(batch.state, "draft")

    def test_choke_point_is_state_guarded(self):
        batch = self._make_batch()
        batch.candidate_ids.write({"state": "shipped"})
        batch.write({"state": "screening"})
        batch._check_members_settled()
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")

        # A second call is a no-op: the state flip is the double-enqueue
        # guard (the batch is no longer in screening).
        batch._check_members_settled()
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")
