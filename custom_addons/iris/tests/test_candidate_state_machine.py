"""Candidate state-machine tests: every pipeline transition + failure paths.

All LLM calls are mocked at the mixin's import path (see ``common.mock_llm``)
and processed through the real queue cron, exactly like production.
"""

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm


@tagged("post_install", "-at_install", "iris")
class TestCandidateStateMachine(IrisCase):
    # ------------------------------------------------------------------
    # Screening guards
    # ------------------------------------------------------------------
    def test_screen_requires_resume_text(self):
        candidate = self._make_candidate(resume_text=False)
        with self.assertRaises(UserError):
            candidate.action_screen()
        self.env.invalidate_all()
        self.assertEqual(candidate.state, "draft")

    def test_screen_requires_draft_state(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        self.assertEqual(candidate.state, "shipped")
        with self.assertRaises(UserError):
            candidate.action_screen()

    # ------------------------------------------------------------------
    # Verdict-driven transitions
    # ------------------------------------------------------------------
    def test_ship_flow(self):
        candidate = self._make_candidate()
        candidate.action_screen()
        self.assertEqual(candidate.state, "screening")
        screening = candidate.screening_ids
        self.assertEqual(screening.llm_status, "queued")

        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()

        self.assertEqual(candidate.state, "shipped")
        self.assertEqual(screening.llm_status, "done")
        self.assertEqual(screening.verdict, "ship")
        self.assertFalse(screening.verdict_manual)
        self.assertFalse(candidate.hold_deadline)
        self.assertEqual(candidate.current_screening_id, screening)
        self.assertEqual(candidate.current_verdict, "ship")

    def test_hold_flow_sets_deadline(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_HOLD_RECORD)
        self.assertEqual(candidate.state, "hold")
        self.assertEqual(screening.verdict, "hold")
        self.assertTrue(candidate.hold_deadline)
        self.assertEqual(screening.hold_deadline, candidate.hold_deadline)

    def test_block_flow(self):
        # v1.1: an LLM BLOCK no longer applies directly — it parks in
        # pending_block until a DIFFERENT screener co-signs (dual control).
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_BLOCK_RECORD)
        self.assertEqual(candidate.state, "pending_block")
        self.assertEqual(screening.verdict, "block")
        self.assertEqual(screening.block_signoff_state, "pending")
        candidate.with_user(self.user_second)._block_signoff("credibility")
        self.assertEqual(candidate.state, "blocked")
        self.assertFalse(candidate.hold_deadline)

    # ------------------------------------------------------------------
    # needs_review + manual verdict
    # ------------------------------------------------------------------
    def test_unparseable_routes_to_needs_review(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.UNPARSEABLE_RECORD)
        self.assertEqual(candidate.state, "needs_review")
        self.assertEqual(screening.llm_status, "needs_review")
        self.assertFalse(screening.verdict)
        # The raw record is still persisted for the manager to read.
        self.assertEqual(screening.markdown_record, self.UNPARSEABLE_RECORD)

    def test_manager_manual_verdict_ship(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.UNPARSEABLE_RECORD)
        candidate.with_user(self.user_manager).action_manual_verdict_ship()
        self.assertEqual(candidate.state, "shipped")
        self.assertEqual(screening.llm_status, "done")
        self.assertEqual(screening.verdict, "ship")
        self.assertTrue(screening.verdict_manual)

    def test_manual_verdict_hold_sets_deadline(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.UNPARSEABLE_RECORD)
        candidate.with_user(self.user_manager).action_manual_verdict_hold()
        self.assertEqual(candidate.state, "hold")
        self.assertTrue(candidate.hold_deadline)

    def test_manual_verdict_requires_manager(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.UNPARSEABLE_RECORD)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_iris).action_manual_verdict_ship()
        self.env.invalidate_all()
        self.assertEqual(candidate.state, "needs_review")

    def test_manual_verdict_requires_needs_review_state(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        with self.assertRaises(UserError):
            candidate.with_user(self.user_manager).action_manual_verdict_block()

    # ------------------------------------------------------------------
    # Interview guide
    # ------------------------------------------------------------------
    def test_guide_flow_shipped_to_interview_ready(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        candidate.action_generate_guide()
        interview = candidate.interview_ids
        self.assertEqual(interview.llm_status, "queued")
        self.assertEqual(interview.screening_id, candidate.current_screening_id)

        with mock_llm("# Interview Guide\n\nSteering Ladder questions..."):
            self._run_llm_queue()

        self.assertEqual(candidate.state, "interview_ready")
        self.assertEqual(interview.llm_status, "done")
        self.assertIn("Steering Ladder", interview.guide_markdown)
        self.assertTrue(interview.attachment_id)

    def test_guide_requires_shipped_state(self):
        candidate = self._make_candidate()
        with self.assertRaises(UserError):
            candidate.action_generate_guide()

    def test_guide_failure_reverts_to_shipped(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        candidate.action_generate_guide()
        with mock_llm(side_effect=Exception("gateway exploded")):
            self._run_llm_queue()
        interview = candidate.interview_ids
        self.assertEqual(interview.llm_status, "failed")
        self.assertIn("gateway exploded", interview.llm_error)
        self.assertEqual(candidate.state, "shipped")

    # ------------------------------------------------------------------
    # Notes → scorecard → final decision
    # ------------------------------------------------------------------
    def _interview_ready_candidate(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        candidate.action_generate_guide()
        with mock_llm("# Interview Guide\n\nSteering Ladder..."):
            self._run_llm_queue()
        return candidate, candidate.interview_ids

    def test_notes_submission_moves_to_interviewed(self):
        candidate, interview = self._interview_ready_candidate()
        interview.write({"notes": "Q1 5 caught R1; Q2 4; no red flags."})
        interview.action_submit_notes()
        self.assertEqual(candidate.state, "interviewed")
        self.assertTrue(interview.notes_submitted)
        self.assertTrue(interview.interviewed_at)

    def test_empty_notes_cannot_be_submitted(self):
        _candidate, interview = self._interview_ready_candidate()
        with self.assertRaises(UserError):
            interview.action_submit_notes()

    def test_scorecard_flow_to_scored_then_hired(self):
        candidate, interview = self._interview_ready_candidate()
        interview.write({"notes": "Q1 5; Q2 4; rederived latency budget."})
        interview.action_submit_notes()

        candidate.action_generate_scorecard()
        scorecard = interview.scorecard_ids
        self.assertEqual(scorecard.llm_status, "queued")
        self.assertEqual(scorecard.notes_snapshot, interview.notes)

        with mock_llm(self.VALID_SCORECARD_STRONG_HIRE):
            self._run_llm_queue()

        self.assertEqual(scorecard.llm_status, "done")
        self.assertEqual(scorecard.recommendation, "strong_hire")
        self.assertEqual(candidate.state, "scored")
        self.assertEqual(candidate.final_recommendation, "strong_hire")

        candidate.action_mark_hired()
        self.assertEqual(candidate.state, "hired")

    def test_mark_rejected_from_scored(self):
        candidate, interview = self._interview_ready_candidate()
        interview.write({"notes": "Q1 2; Q2 2; missed every breadcrumb."})
        interview.action_submit_notes()
        candidate.action_generate_scorecard()
        with mock_llm(self.VALID_SCORECARD_NO_HIRE):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "scored")
        candidate.action_mark_rejected()
        self.assertEqual(candidate.state, "rejected")

    def test_final_decision_requires_scored_state(self):
        candidate = self._make_candidate()
        with self.assertRaises(UserError):
            candidate.action_mark_hired()
        with self.assertRaises(UserError):
            candidate.action_mark_rejected()

    def test_scorecard_failure_reverts_to_interviewed(self):
        candidate, interview = self._interview_ready_candidate()
        interview.write({"notes": "terse notes"})
        interview.action_submit_notes()
        candidate.action_generate_scorecard()
        with mock_llm(side_effect=Exception("scorecard boom")):
            self._run_llm_queue()
        self.assertEqual(interview.scorecard_ids.llm_status, "failed")
        self.assertEqual(candidate.state, "interviewed")

    # ------------------------------------------------------------------
    # LLM failure + missing key
    # ------------------------------------------------------------------
    def test_llm_exception_marks_failed_and_reverts_to_draft(self):
        candidate = self._make_candidate()
        candidate.action_screen()
        with mock_llm(side_effect=Exception("network is on fire")):
            self._run_llm_queue()
        screening = candidate.screening_ids
        self.assertEqual(screening.llm_status, "failed")
        self.assertIn("network is on fire", screening.llm_error)
        self.assertEqual(candidate.state, "draft")

    def test_missing_api_key_raises_and_nothing_sticks(self):
        candidate = self._make_candidate()
        self._clear_api_key()
        with self.assertRaises(UserError):
            candidate.action_screen()
        self.env.invalidate_all()
        # assertRaises rolls the savepoint back: the candidate is still
        # draft and no artifact is stuck in queued/running.
        self.assertEqual(candidate.state, "draft")
        self.assertFalse(candidate.screening_ids)
        self.assertFalse(self.env["iris.screening"].search([
            ("candidate_id", "=", candidate.id),
            ("llm_status", "in", ("queued", "running")),
        ]))

    def test_retry_failed_screening(self):
        candidate = self._make_candidate()
        candidate.action_screen()
        with mock_llm(side_effect=Exception("boom")):
            self._run_llm_queue()
        screening = candidate.screening_ids
        self.assertEqual(screening.llm_status, "failed")

        screening.action_retry_llm()
        self.assertEqual(screening.llm_status, "queued")
        self.assertEqual(candidate.state, "screening")
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "shipped")
        self.assertEqual(screening.verdict, "ship")
