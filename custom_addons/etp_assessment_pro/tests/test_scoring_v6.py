# -*- coding: utf-8 -*-
"""Local, fully-mocked tests for the v6 subjective scoring redesign.

NO live LLM calls: every Vertex call is patched with a fixed JSON payload, so
these run offline and deterministically. They lock the three guarantees the
redesign provides:

1. The grader's raw 0-100 score is stored immutably (llm_raw_100) and pass/fail
   + the earned mark are COMPUTED from it against the live Settings threshold.
2. Changing the threshold in Settings RE-DECIDES pass/fail for already-scored
   answers with no re-scoring (the live flip).
3. A scoring call/parse failure is SURFACED as llm_state='error' (not a silent
   scored-0), and the subjective max never drifts (always 1 per answer).

The question types are exercised: mcq + msq (deterministic, no LLM) and the LLM
types (subjective_rubric, subjective_rubric, image_ab, image_prompt;
image_label shares the image_prompt scoring path).
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex, scoring


class _ScoringBase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.AB_OPTS = ["Response A", "Response B", "Both Good", "Both Bad", "Tie"]
        # Per-answer threshold is now the per-assessment field (defaults to 70).

    def _evaluator(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        category = self.env["etp.assessment.pro.prompt"].create(
            {"name": "Scoring Cat"})
        assessment = self.Assessment.create({
            "name": "Scoring Assessment", "generator_id": category.id})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
        })
        return ev, applicant, assessment

    def _opt(self, qd, label):
        return qd.option_line_ids.filtered(lambda o: o.name == label)[:1]

    def _attach_dim(self, question, axis_name, correct_label, options=None):
        options = options or self.AB_OPTS
        return self.QDim.create({
            "question_id": question.id,
            "name": axis_name,
            "option_line_ids": [
                (0, 0, {"name": o, "sequence": (i + 1) * 10,
                        "is_correct": o == correct_label})
                for i, o in enumerate(options)
            ],
        })

    def _resp(self, ev, applicant, assessment, question,
              justification="", lines=None):
        vals = {
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": question.id,
            "justification": justification,
        }
        if lines:
            vals["line_ids"] = lines
        return self.Response.create(vals)

    def _mock_score(self, resp, fixed_results):
        """Run the unified scorer with the Vertex call mocked to return the
        subjective-judge-v6 wrapper object around fixed_results (a python list of
        per-item result dicts, scores as 0.00-1.00 floats)."""
        payload = json.dumps({
            "schema_version": "subjective-judge-v6",
            "pass_threshold": 0.70,
            "submission_flags": [],
            "results": fixed_results,
        })
        with patch.object(vertex, "_call_vertex", return_value=payload):
            return scoring._score_submission(self.env, resp)


class TestSubjectiveTypesScored(_ScoringBase):
    def test_subjective_rubric_scored_and_stored(self):
        q = self.Question.create({
            "name": "Justify",
            "prompt": "Justify your verdict.",
            "question_type": "subjective_rubric",
        })
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q,
                          justification="A is sharper, evidence: crisp edges.")
        self.assertTrue(resp.needs_llm)
        n = self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.82, "passed": True, "rubric_source": "generated",
            "rubric": {}, "gate": "none",
            "reference_answer": "A is sharper because...",
            "reasoning": "Point c1 met by 'crisp edges'.",
            "verdict_consistency": "match", "feedback": "Solid.", "flags": [],
        }])
        resp.invalidate_recordset()
        self.assertEqual(n, 1)
        self.assertEqual(resp.llm_state, "scored")
        # raw stored immutably; mark + pass computed from it vs threshold 70.
        self.assertAlmostEqual(resp.llm_raw_100, 82.0)
        self.assertAlmostEqual(resp.llm_raw_score, 0.82)
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.llm_score, 1)
        self.assertEqual(resp.llm_max_score, 1)
        self.assertEqual(resp.subjective_result, "pass")
        # SOP audit captured.
        self.assertEqual(resp.llm_rubric_source, "generated")
        self.assertEqual(resp.llm_gate, "none")
        self.assertTrue(resp.llm_reference_answer)
        self.assertTrue(resp.llm_result_json)
        # v6 audit fields ride along in llm_result_json (never read for decisions).
        stored = json.loads(resp.llm_result_json)
        self.assertEqual(stored["field_key"], "justification")
        self.assertEqual(stored["verdict_consistency"], "match")
        self.assertIn("passed", stored)
        self.assertIn("skills", stored)

    def test_subjective_rubric_scored(self):
        q = self.Question.create({
            "name": "Rubric Q",
            "prompt": "Answer per rubric.",
            "question_type": "subjective_rubric",
            "subjective_rubric_json": json.dumps({
                "checklist": ["names a specific feature"],
                "constraints": ["stays on topic"],
                "pass_condition": "endorses A",
            }),
        })
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="A wins, sharper.")
        # v6 says passed=true (advisory), but 0.65 -> 65 < live threshold 70.
        n = self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.65, "passed": True, "rubric_source": "supplied",
            "rubric": {"checklist": ["names a specific feature"],
                       "constraints": ["stays on topic"],
                       "pass_condition": "endorses A"},
            "gate": "none", "reference_answer": "...", "reasoning": "...",
            "verdict_consistency": "match", "feedback": "Close.", "flags": [],
        }])
        resp.invalidate_recordset()
        self.assertEqual(n, 1)
        # 65 < 70 -> fail under default threshold, but state is scored (genuine).
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 65.0)
        # The v6 advisory `passed` is IGNORED; the live threshold decides FAIL.
        stored = json.loads(resp.llm_result_json)
        self.assertTrue(stored["passed"])        # advisory says pass...
        self.assertFalse(resp.llm_passed)        # ...live threshold says fail
        self.assertEqual(resp.llm_score, 0)
        self.assertEqual(resp.subjective_result, "fail")

    def test_image_prompt_scored(self):
        q = self.Question.create({
            "name": "Describe",
            "prompt": "Describe the image.",
            "question_type": "image_prompt",
            "subjective_rubric_json": json.dumps({
                "ideal_prompt": "A fluffy cat on a sofa.",
                "mandatory_elements": ["cat"],
                "penalty_rules": ["no hallucinated objects"],
                "scoring_guide": "Award for accuracy.",
            }),
        })
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q,
                          justification="A fluffy cat on a sofa.")
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.90, "passed": True, "rubric_source": "supplied",
            "rubric": {"checklist": ["names the cat"], "constraints": [],
                       "pass_condition": "captures the scene"},
            "gate": "none", "reference_answer": "A fluffy cat...",
            "reasoning": "mandatory 'cat' present.",
            "verdict_consistency": "match", "feedback": "Accurate.", "flags": [],
        }])
        resp.invalidate_recordset()
        self.assertAlmostEqual(resp.llm_raw_100, 90.0)
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.llm_max_score, 1)

    def test_image_ab_scored_generic_over_axes(self):
        q = self.Question.create({
            "name": "AB",
            "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction.",
        })
        self._attach_dim(q, "Instruction Following (IF)", "Response A")
        self._attach_dim(q, "Overall Choice (OC)", "Response A")
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        lines = [
            (0, 0, {"question_dimension_id": qd.id,
                    "selected_option_id": self._opt(qd, "Response A").id})
            for qd in q.question_dimension_ids
        ]
        resp = self._resp(ev, app, ass, q,
                          justification="A is sharper and on-instruction.",
                          lines=lines)
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.88, "passed": True, "rubric_source": "generated",
            "rubric": {}, "gate": "none", "reference_answer": "A...",
            "reasoning": "axes match", "verdict_consistency": "match",
            "feedback": "Strong.", "flags": [], "alignment": "high",
        }])
        resp.invalidate_recordset()
        # blend: 0.75*verdict(1.0) + 0.25*justification(0.88) = 0.97 -> raw 97
        self.assertAlmostEqual(resp.llm_raw_100, 97.0)
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.subjective_result, "pass")


class TestObjectiveTypesNoLLM(_ScoringBase):
    def test_mcq_msq_are_code_scored_not_llm(self):
        for qtype in ("mcq", "msq"):
            q = self.Question.create({
                "name": "Obj %s" % qtype,
                "prompt": "Pick.",
                "question_type": qtype,
            })
            qd = self._attach_dim(q, "Instruction Following (IF)", "Response A")
            ev, app, ass = self._evaluator()
            lines = [(0, 0, {
                "question_dimension_id": qd.id,
                "selected_option_id": self._opt(qd, "Response A").id})]
            resp = self._resp(ev, app, ass, q, lines=lines)
            resp.invalidate_recordset()
            self.assertTrue(resp.has_objective)
            self.assertFalse(resp.needs_llm)
            self.assertEqual(resp.score, 1)
            self.assertEqual(resp.max_score, 1)


class TestLiveThresholdFlip(_ScoringBase):
    def test_threshold_change_reflips_pass_fail_without_rescoring(self):
        q = self.Question.create({
            "name": "Justify",
            "prompt": "Justify.",
            "question_type": "subjective_rubric",
        })
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="A wins because X.")
        # Score once at raw 75.
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.75, "passed": True, "rubric_source": "generated",
            "rubric": {}, "gate": "none", "reference_answer": "x",
            "reasoning": "x", "verdict_consistency": "match",
            "feedback": "x", "flags": [],
        }])
        resp.invalidate_recordset()
        self.assertTrue(resp.llm_passed)          # 75 >= 70
        self.assertEqual(resp.subjective_result, "pass")

        # Raise this assessment's threshold to 80; NO re-scoring.
        ass.subjective_threshold = 80.0
        resp.invalidate_recordset()
        self.assertAlmostEqual(resp.llm_raw_100, 75.0)   # raw is untouched
        self.assertFalse(resp.llm_passed)                # 75 < 80 now -> fail
        self.assertEqual(resp.subjective_result, "fail")
        self.assertEqual(resp.llm_score, 0)

        # Lower it to 70 again; the same stored raw now passes again.
        ass.subjective_threshold = 70.0
        resp.invalidate_recordset()
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.subjective_result, "pass")


class TestErrorSurfacingAndMaxStability(_ScoringBase):
    def test_call_failure_surfaces_error_not_silent_zero(self):
        q = self.Question.create({
            "name": "Justify", "prompt": "Justify.",
            "question_type": "subjective_rubric"})
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="some answer")
        self.ICP.set_param("etp_assessment_pro.llm_max_attempts", "1")

        def _boom(*a, **k):
            raise RuntimeError("vertex exploded")

        with patch.object(vertex, "_call_vertex", side_effect=_boom):
            scoring._score_submission(self.env, resp)
        resp.invalidate_recordset()
        # Surfaced as error, NOT a clean scored-0.
        self.assertEqual(resp.llm_state, "error")
        self.assertNotEqual(resp.llm_state, "scored")
        self.assertFalse(resp.llm_passed)
        self.assertEqual(resp.llm_score, 0)
        self.assertEqual(resp.llm_max_score, 1)   # max never drifts
        self.assertIn("error", (resp.llm_feedback or "").lower())

    def test_missing_id_in_response_surfaces_error(self):
        q = self.Question.create({
            "name": "Justify", "prompt": "Justify.",
            "question_type": "subjective_rubric"})
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="some answer")
        self.ICP.set_param("etp_assessment_pro.llm_max_attempts", "1")
        # Grader returns a different id -> miss -> error (after 1 attempt).
        self._mock_score(resp, [{
            "item_id": str(resp.id + 9999), "id": resp.id + 9999,
            "score": 0.90, "passed": True}])
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "error")
        self.assertEqual(resp.llm_max_score, 1)

    def test_max_score_stable_at_one_through_lifecycle(self):
        q = self.Question.create({
            "name": "Justify", "prompt": "Justify.",
            "question_type": "subjective_rubric"})
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="answer")
        # Before scoring: needs_llm, max already 1 (computed), not 10.
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_max_score, 1)
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.95, "passed": True, "gate": "none",
            "rubric_source": "generated", "rubric": {}, "reference_answer": "x",
            "reasoning": "x", "verdict_consistency": "match",
            "feedback": "x", "flags": []}])
        resp.invalidate_recordset()
        # After scoring: still 1. No 10 -> 1 drift.
        self.assertEqual(resp.llm_max_score, 1)


class TestGatedAnswer(_ScoringBase):
    def test_gated_answer_scores_zero_and_fails(self):
        q = self.Question.create({
            "name": "Justify", "prompt": "Justify.",
            "question_type": "subjective_rubric"})
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="na")
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.0, "passed": False, "gate": "placeholder_answer",
            "rubric_source": "generated", "rubric": {}, "reference_answer": "",
            "reasoning": "Not evaluated: placeholder 'na'.",
            "verdict_consistency": "not_applicable",
            "feedback": "Gated.", "flags": []}])
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertEqual(resp.llm_gate, "placeholder_answer")
        self.assertAlmostEqual(resp.llm_raw_100, 0.0)
        self.assertFalse(resp.llm_passed)
        self.assertEqual(resp.subjective_result, "fail")
