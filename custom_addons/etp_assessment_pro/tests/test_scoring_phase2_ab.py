# -*- coding: utf-8 -*-
"""Phase 2 two-lane image_ab scoring tests (fully mocked, offline).

image_ab is scored on two lanes composed into the single immutable llm_raw_100:

1. VERDICT lane (deterministic, NO LLM): the mean exact-match of the candidate's
   per-dimension picks against the keyed correct options, 0..1.
2. JUSTIFICATION lane (LLM, 0..1): only when the assessment requires a
   justification AND the candidate wrote one (``_image_ab_uses_llm``).

When the justification lane is active the two blend as
``0.75*verdict + 0.25*justification`` (AB_VERDICT_WEIGHT / AB_JUSTIFICATION_WEIGHT)
BEFORE the single _store_scored write; otherwise llm_raw_100 = verdict*100 and NO
Vertex call is made. Sub-scores live in the llm_result_json audit only. Pass/fail
still derives live from the per-assessment threshold (Phase 1 invariant).
"""
import json
from unittest.mock import patch

from odoo.addons.etp_assessment_pro.services import vertex, scoring

from .test_scoring_v6 import _ScoringBase


class TestPhase2ABTwoLane(_ScoringBase):
    def _ab_question(self, name, axes):
        """axes: list of (axis_name, correct_label)."""
        q = self.Question.create({
            "name": name,
            "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction.",
        })
        for axis_name, correct in axes:
            self._attach_dim(q, axis_name, correct)
        return q

    def _lines_picking(self, q, picks):
        """picks: {axis_name: chosen_label}."""
        return [
            (0, 0, {"question_dimension_id": qd.id,
                    "selected_option_id": self._opt(qd, picks[qd.name]).id})
            for qd in q.question_dimension_ids
        ]

    # (a) 4/4 verdicts correct, no justification -> raw 100, no Vertex call.
    def test_a_four_of_four_no_justification_raw_100(self):
        axes = [
            ("Instruction Following (IF)", "Response A"),
            ("Visual Quality (VQ)", "Response B"),
            ("Label Accuracy (LAI)", "Both Good"),
            ("Overall Choice (OC)", "Response A"),
        ]
        q = self._ab_question("AB-4of4", axes)
        ev, app, ass = self._evaluator()
        lines = self._lines_picking(q, {a: c for a, c in axes})
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        self.assertTrue(resp.needs_llm)
        self.assertFalse(resp._image_ab_uses_llm())
        with patch.object(vertex, "_call_vertex") as m:
            n = scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(n, 1)
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)
        stored = json.loads(resp.llm_result_json)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 1.0)
        self.assertIsNone(stored["ab_scores"]["justification_score"])
        self.assertAlmostEqual(stored["ab_scores"]["blend"], 1.0)

    # (b) 2/4 verdicts correct, no justification -> raw 50.
    def test_b_two_of_four_no_justification_raw_50(self):
        axes = [
            ("Instruction Following (IF)", "Response A"),
            ("Visual Quality (VQ)", "Response B"),
            ("Label Accuracy (LAI)", "Both Good"),
            ("Overall Choice (OC)", "Response A"),
        ]
        q = self._ab_question("AB-2of4", axes)
        ev, app, ass = self._evaluator()
        lines = self._lines_picking(q, {
            "Instruction Following (IF)": "Response A",  # correct
            "Visual Quality (VQ)": "Response A",         # wrong (key B)
            "Label Accuracy (LAI)": "Both Good",         # correct
            "Overall Choice (OC)": "Both Bad",           # wrong (key A)
        })
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 50.0)
        stored = json.loads(resp.llm_result_json)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 0.5)
        self.assertIsNone(stored["ab_scores"]["justification_score"])

    # (c) all verdicts correct + judge scores justification 0.2 -> blend 0.80.
    def test_c_all_correct_plus_justification_blend_raw_80(self):
        axes = [
            ("Instruction Following (IF)", "Response A"),
            ("Overall Choice (OC)", "Response B"),
        ]
        q = self._ab_question("AB-blend", axes)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        lines = self._lines_picking(q, {a: c for a, c in axes})
        resp = self._resp(
            ev, app, ass, q,
            justification="A is on-instruction; B is sharper overall.",
            lines=lines)
        self.assertTrue(resp._image_ab_uses_llm())
        self._mock_score(resp, [{
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": 0.20, "passed": False, "rubric_source": "generated",
            "rubric": {}, "gate": "none", "reference_answer": "A...",
            "reasoning": "thin justification", "verdict_consistency": "match",
            "feedback": "Weak.", "flags": [],
        }])
        resp.invalidate_recordset()
        # 0.75*verdict(1.0) + 0.25*justification(0.2) = 0.80 -> raw 80.
        self.assertAlmostEqual(resp.llm_raw_100, 80.0)
        stored = json.loads(resp.llm_result_json)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 1.0)
        self.assertAlmostEqual(stored["ab_scores"]["justification_score"], 0.2)
        self.assertAlmostEqual(stored["ab_scores"]["blend"], 0.80)
        self.assertEqual(stored["ab_scores"]["verdict_weight"], 0.75)
        self.assertEqual(stored["ab_scores"]["justification_weight"], 0.25)

    # (d) verdict-only image_ab with blank justification: NO Vertex call even
    #     when the assessment requires a justification.
    def test_d_blank_justification_skips_vertex(self):
        axes = [("Instruction Following (IF)", "Response A")]
        q = self._ab_question("AB-blank", axes)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        lines = self._lines_picking(q, {a: c for a, c in axes})
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        self.assertFalse(resp._image_ab_uses_llm())
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)

    # (e) sub-scores land in llm_result_json (audit only, no new stored field),
    #     via the higher-level score_evaluator entry point.
    def test_e_subscores_in_result_json_via_evaluator(self):
        axes = [
            ("Instruction Following (IF)", "Response A"),
            ("Overall Choice (OC)", "Response B"),
        ]
        q = self._ab_question("AB-audit", axes)
        ev, app, ass = self._evaluator()
        lines = self._lines_picking(q, {
            "Instruction Following (IF)": "Response A",  # correct
            "Overall Choice (OC)": "Response A",         # wrong (key B)
        })
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        with patch.object(vertex, "_call_vertex") as m:
            scored = scoring.score_evaluator(self.env, ev)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(scored, 1)
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 50.0)
        stored = json.loads(resp.llm_result_json)
        self.assertIn("ab_scores", stored)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 0.5)
        self.assertAlmostEqual(stored["ab_scores"]["blend"], 0.5)
        # No new mutable stored sub-score field: audit lives only in the JSON.
        self.assertFalse(hasattr(type(resp), "ab_verdict_score"))
        self.assertFalse(hasattr(type(resp), "ab_justification_score"))
