"""Clarifying questions (P2-9): one-shot LLM sub-artifact of a HOLD screening.

Contract under test (models/iris_clarification.py + iris_screening
``action_generate_clarifying_questions`` + the evidence wizard):

* guards: HOLD verdict + candidate still on Hold + non-empty record;
* each generation is a NEW ``iris.clarification`` row (audit preserved);
  the latest successful set is denormalized onto
  ``screening.clarifying_questions_markdown`` (regeneration overwrites);
* the evidence wizard's ``default_get`` surfaces the questions so the
  candidate's written answers land in ``verification_evidence``;
* a generation failure leaves the candidate on Hold and the denormalized
  field untouched (chatter only);
* the API serializer exposes ``clarifying_questions`` in full screening
  payloads only.
"""

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm
from odoo.addons.iris.controllers.common import _screening_dict


@tagged("post_install", "-at_install", "iris")
class TestClarifyingQuestions(IrisCase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _hold_candidate(self, name="Jane Doe"):
        candidate = self._make_candidate(name=name)
        self._screen(candidate, self.VALID_HOLD_RECORD)
        self.assertEqual(candidate.state, "hold")
        return candidate

    def _generate(self, candidate, content=None):
        clarification = candidate.action_generate_clarifying_questions()
        with mock_llm(content or self.VALID_CLARIFYING_QUESTIONS):
            self._run_llm_queue()
        self.assertEqual(clarification.llm_status, "done")
        return clarification

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def test_non_hold_candidate_rejected(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        with self.assertRaises(UserError):
            candidate.action_generate_clarifying_questions()
        self.env.invalidate_all()
        self.assertFalse(self.env["iris.clarification"].search([
            ("candidate_id", "=", candidate.id),
        ]))

    def test_draft_candidate_rejected(self):
        candidate = self._make_candidate()
        with self.assertRaises(UserError):
            candidate.action_generate_clarifying_questions()

    def test_ship_screening_rejected_even_when_called_directly(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        with self.assertRaises(UserError):
            screening.action_generate_clarifying_questions()

    def test_hold_screening_with_moved_on_candidate_rejected(self):
        candidate = self._hold_candidate()
        screening = candidate._get_current_hold_screening()
        candidate.write({"state": "shipped"})
        with self.assertRaises(UserError):
            screening.action_generate_clarifying_questions()

    def test_empty_screening_record_rejected(self):
        candidate = self._hold_candidate()
        screening = candidate._get_current_hold_screening()
        screening.write({"markdown_record": False})
        with self.assertRaises(UserError):
            screening.action_generate_clarifying_questions()

    # ------------------------------------------------------------------
    # Happy path: artifact + denormalized field
    # ------------------------------------------------------------------
    def test_generation_populates_artifact_and_denormalized_field(self):
        candidate = self._hold_candidate()
        screening = candidate._get_current_hold_screening()

        clarification = candidate.action_generate_clarifying_questions()
        self.assertEqual(clarification.llm_status, "queued")
        self.assertEqual(clarification.screening_id, screening)
        self.assertEqual(clarification.candidate_id, candidate)
        self.assertFalse(screening.clarifying_questions_markdown)

        with mock_llm(self.VALID_CLARIFYING_QUESTIONS):
            self._run_llm_queue()

        self.assertEqual(clarification.llm_status, "done")
        self.assertEqual(
            clarification.questions_markdown,
            self.VALID_CLARIFYING_QUESTIONS,
        )
        self.assertEqual(
            screening.clarifying_questions_markdown,
            self.VALID_CLARIFYING_QUESTIONS,
        )
        # No state machine is touched by this sub-artifact.
        self.assertEqual(candidate.state, "hold")
        self.assertEqual(screening.verdict, "hold")

        bodies = self._chatter_bodies(candidate)
        self.assertTrue(
            any("Clarifying questions generated" in body for body in bodies),
            f"no generation note found in: {bodies}",
        )

        # The HOLD record is fenced into the prompt as untrusted data.
        prompt = clarification.llm_prompt_input
        self.assertIn("HOLD SCREENING RECORD:", prompt)
        self.assertIn("BEGIN HOLD SCREENING RECORD>>>", prompt)
        self.assertIn("serving 40M queries/day", prompt)
        self.assertIn("TARGET ROLE / LEVEL:", prompt)

    def test_regeneration_overwrites_denormalized_field_keeping_artifacts(self):
        candidate = self._hold_candidate()
        screening = candidate._get_current_hold_screening()
        first = self._generate(candidate)

        second_set = (
            self.VALID_CLARIFYING_QUESTIONS
            + "4. What was the size of the team at Globex?\n"
        )
        second = candidate.action_generate_clarifying_questions()
        self.assertNotEqual(second, first)
        with mock_llm(second_set):
            self._run_llm_queue()

        # Latest set wins on the screening; both artifact rows keep their own.
        self.assertEqual(screening.clarifying_questions_markdown, second_set)
        self.assertEqual(
            first.questions_markdown, self.VALID_CLARIFYING_QUESTIONS,
        )
        self.assertEqual(second.questions_markdown, second_set)
        self.assertEqual(len(screening.clarification_ids), 2)
        self.assertEqual(first.llm_status, "done")

    # ------------------------------------------------------------------
    # Evidence wizard integration
    # ------------------------------------------------------------------
    def test_evidence_wizard_default_get_shows_questions(self):
        candidate = self._hold_candidate()
        self._generate(candidate)

        Wizard = self.env["iris.evidence.wizard"].with_context(
            default_candidate_id=candidate.id,
        )
        defaults = Wizard.default_get(
            ["candidate_id", "clarifying_questions", "evidence", "rescreen_now"],
        )
        self.assertEqual(defaults.get("candidate_id"), candidate.id)
        self.assertEqual(
            defaults.get("clarifying_questions"),
            self.VALID_CLARIFYING_QUESTIONS,
        )

    def test_evidence_wizard_default_get_without_questions(self):
        candidate = self._hold_candidate()
        Wizard = self.env["iris.evidence.wizard"].with_context(
            default_candidate_id=candidate.id,
        )
        defaults = Wizard.default_get(["candidate_id", "clarifying_questions"])
        self.assertFalse(defaults.get("clarifying_questions"))

    # ------------------------------------------------------------------
    # Failure path
    # ------------------------------------------------------------------
    def test_failure_leaves_candidate_on_hold(self):
        candidate = self._hold_candidate()
        screening = candidate._get_current_hold_screening()
        clarification = candidate.action_generate_clarifying_questions()

        with mock_llm(side_effect=Exception("questions boom")):
            self._run_llm_queue()

        self.assertEqual(clarification.llm_status, "failed")
        self.assertIn("questions boom", clarification.llm_error)
        self.assertEqual(candidate.state, "hold")
        self.assertEqual(screening.verdict, "hold")
        self.assertFalse(screening.clarifying_questions_markdown)
        bodies = self._chatter_bodies(candidate)
        self.assertTrue(
            any("Clarifying-question generation failed" in body for body in bodies),
            f"no failure note found in: {bodies}",
        )

        # Retry is failed-only, then re-queues through the same artifact.
        clarification.action_retry_llm()
        self.assertEqual(clarification.llm_status, "queued")
        with mock_llm(self.VALID_CLARIFYING_QUESTIONS):
            self._run_llm_queue()
        self.assertEqual(clarification.llm_status, "done")
        self.assertEqual(
            screening.clarifying_questions_markdown,
            self.VALID_CLARIFYING_QUESTIONS,
        )
        with self.assertRaises(UserError):
            clarification.action_retry_llm()

    # ------------------------------------------------------------------
    # Serializer exposure
    # ------------------------------------------------------------------
    def test_serializer_exposes_clarifying_questions_when_full(self):
        candidate = self._hold_candidate()
        self._generate(candidate)
        screening = candidate._get_current_hold_screening()

        full = _screening_dict(screening, full=True)
        self.assertEqual(
            full["clarifying_questions"], self.VALID_CLARIFYING_QUESTIONS,
        )
        summary = _screening_dict(screening)
        self.assertNotIn("clarifying_questions", summary)
