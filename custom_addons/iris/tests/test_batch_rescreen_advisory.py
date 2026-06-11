"""Advisory re-screens from a batch consistency finding (v1.1).

``candidate.action_rescreen_advisory()`` is the ONLY way a consistency
finding changes a verdict — and it goes through the normal evidence-gated
re-screen pipeline: manager-only, available for shipped/blocked members of
a DONE batch, and the batch finding itself is written as the recorded
verification evidence (honoring "no evidence, no re-screen"). The new
screening chains to the prior one with ``rescreen_reason =
'batch_consistency'`` and its verdict applies through ``_apply_verdict``
like any other screening.
"""

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import DEFAULT_LLM_RESULT, IrisCase, mock_llm


def _result(content):
    return dict(DEFAULT_LLM_RESULT, content=content)


@tagged("post_install", "-at_install", "iris")
class TestBatchRescreenAdvisory(IrisCase):
    def _done_batch(self, contents=None):
        """A batch driven all the way to ``done`` (default: 2 SHIP members).

        The consistency pass completes with the unparseable report — the
        fail-open path still lands the batch in ``done``, which is all the
        advisory re-screen guard cares about.
        """
        batch = self._make_batch()
        batch.action_screen_batch()
        contents = contents or [self.VALID_SHIP_RECORD, self.VALID_SHIP_RECORD]
        with mock_llm(side_effect=[_result(content) for content in contents]):
            self._run_llm_queue()
        self.assertEqual(batch.state, "consistency")
        with mock_llm(self.UNPARSEABLE_BATCH_REPORT):
            self._run_llm_queue()
        self.assertEqual(batch.state, "done")
        return batch

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def test_requires_manager(self):
        batch = self._done_batch()
        member = batch.candidate_ids[0]
        with self.assertRaises(UserError) as ctx:
            member.with_user(self.user_iris).action_rescreen_advisory()
        self.assertIn("Managers", str(ctx.exception))
        self.assertEqual(member.state, "shipped")

    def test_requires_shipped_or_blocked_state(self):
        batch = self._done_batch(
            contents=[self.VALID_HOLD_RECORD, self.VALID_SHIP_RECORD],
        )
        hold_member = batch.candidate_ids.filtered(lambda c: c.state == "hold")
        with self.assertRaises(UserError):
            hold_member.action_rescreen_advisory()

    def test_requires_completed_batch(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        # All members shipped — but the batch is only in consistency.
        self.assertEqual(batch.state, "consistency")
        member = batch.candidate_ids[0]
        with self.assertRaises(UserError) as ctx:
            member.action_rescreen_advisory()
        self.assertIn("batch", str(ctx.exception))

    def test_requires_a_batch(self):
        candidate = self._make_candidate(name="Solo Shipped")
        self._screen(candidate, self.VALID_SHIP_RECORD)
        with self.assertRaises(UserError):
            candidate.action_rescreen_advisory()

    # ------------------------------------------------------------------
    # Happy path: the batch finding IS the recorded evidence
    # ------------------------------------------------------------------
    def test_advisory_rescreen_autofills_evidence_and_chains(self):
        batch = self._done_batch()
        member = batch.candidate_ids[0]
        parent = member.screening_ids.sorted("id")[-1]
        self.assertFalse(parent.verification_evidence)

        rescreen = member.action_rescreen_advisory()

        # "No evidence, no re-screen" honored: the finding was recorded as
        # evidence on the parent screening before the new one was spawned.
        self.assertIn(batch.name, parent.verification_evidence)
        self.assertEqual(parent.evidence_recorded_by, self.env.user)
        self.assertTrue(parent.evidence_recorded_at)

        self.assertTrue(rescreen.is_rescreen)
        self.assertEqual(rescreen.parent_screening_id, parent)
        self.assertEqual(rescreen.rescreen_reason, "batch_consistency")
        self.assertEqual(rescreen.llm_status, "queued")
        self.assertEqual(rescreen.requested_by_id, self.env.user)
        self.assertEqual(member.state, "screening")
        self.assertTrue(any(
            "Advisory re-screen" in body
            for body in self._chatter_bodies(member)
        ))

        # The verdict applies through the normal pipeline (HOLD here —
        # deadline machinery included).
        with mock_llm(self.VALID_HOLD_RECORD):
            self._run_llm_queue()
        self.assertEqual(rescreen.verdict, "hold")
        self.assertEqual(member.state, "hold")
        self.assertTrue(member.hold_deadline)

    def test_advisory_rescreen_prompt_carries_evidence(self):
        batch = self._done_batch()
        member = batch.candidate_ids[0]
        rescreen = member.action_rescreen_advisory()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertIn("VERIFICATION EVIDENCE", rescreen.llm_prompt_input)
        self.assertIn(batch.name, rescreen.llm_prompt_input)
        self.assertIn("PRIOR HOLD RECORD", rescreen.llm_prompt_input)

    def test_existing_evidence_is_not_overwritten(self):
        batch = self._done_batch()
        member = batch.candidate_ids[0]
        parent = member.screening_ids.sorted("id")[-1]
        manual_evidence = "Reference call already on file."
        parent.write({"verification_evidence": manual_evidence})

        member.action_rescreen_advisory()
        self.assertEqual(parent.verification_evidence, manual_evidence,
                         "previously recorded evidence must be preserved")

    def test_blocked_member_can_be_advisory_rescreened(self):
        batch = self._done_batch(
            contents=[self.VALID_SHIP_RECORD, self.VALID_BLOCK_RECORD],
        )
        pending = batch.candidate_ids.filtered(
            lambda c: c.state == "pending_block",
        )
        # pending_block is NOT eligible — the sign-off must complete first.
        with self.assertRaises(UserError):
            pending.action_rescreen_advisory()

        pending.with_user(self.user_second)._block_signoff("credibility")
        self.assertEqual(pending.state, "blocked")

        rescreen = pending.action_rescreen_advisory()
        self.assertEqual(rescreen.rescreen_reason, "batch_consistency")
        self.assertEqual(pending.state, "screening")

    def test_done_batch_state_is_untouched_by_member_rescreen(self):
        batch = self._done_batch()
        member = batch.candidate_ids[0]
        member.action_rescreen_advisory()
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(member.state, "shipped")
        # The completion feeders only watch batches in screening — a late
        # member settle never re-flips a done batch.
        self.assertEqual(batch.state, "done")
