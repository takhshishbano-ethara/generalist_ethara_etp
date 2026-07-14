# -*- coding: utf-8 -*-
"""Phase 3 image_ab FLAW-INJECTION tests (fully mocked, offline).

Flaw injection makes the image_ab answer key ground-truth BY CONSTRUCTION: the
generator plans a clean and a deliberately flawed image and the answer key is
DERIVED from the plan's construction_keys instead of free-form model judgment.
The plan is persisted (flaw_plan_json) on the draft and copied to the bank
question on approve, guarded against key drift at BOTH approve time and score
time. These tests lock:

  (a) a generated image_ab draft materializes an answer key matching its stored
      construction_keys, with the clean/flawed prompts mapped to the right slots;
  (b) approve succeeds and copies the plan when the key matches;
  (c) approve RAISES on a forced key-drift;
  (d) score time: a question whose stored key was tampered vs its flaw_plan_json
      scores 0 with an integrity flag (never silently passes), no Vertex call;
  (e) an existing image_ab with NULL flaw_plan_json scores normally (guards
      no-op, no regression).
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

from odoo.addons.etp_assessment_pro.services import vertex, scoring
from odoo.addons.etp_assessment_pro.constants import (
    ab_code_from_label, ab_specs_from_construction_keys)

from .test_scoring_v6 import _ScoringBase


def _plan(flawed_side="b"):
    """A valid flaw plan: the clean side wins OC and no dimension names the
    flawed side, so validate_flaw_plan accepts it."""
    clean = "Response A" if flawed_side == "b" else "Response B"
    return {
        "flawed_side": flawed_side,
        "clean_prompt": "A kitchen counter with exactly three red apples in a "
                        "white ceramic bowl, soft daylight.",
        "flawed_prompt": "A kitchen counter with four red apples in a white "
                         "ceramic bowl and a spoon floating above them.",
        "injected_flaws": ["four apples instead of three", "a floating spoon"],
        "construction_keys": {
            "IF": clean, "VQ": "Both Good", "LAI": clean, "OC": clean},
    }


class TestFlawInjectionGenerationApprove(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _sop_prompt(self):
        import base64 as _b64
        prompt = self.Prompt.create({"name": "Flaw SOP"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": prompt.id, "name": "sop.pdf",
            "file": _b64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        return prompt

    def _flaw_item(self):
        return {
            "name": "Which render is correct?",
            "prompt": "Two images were produced from the same brief. Pick the "
                      "correct render and explain why.",
            "question_type": "image_ab",
            "difficulty": "medium",
            "image_specs": {"flaw_plan": _plan("b")},
        }

    def _draft_keys(self, draft):
        out = {}
        for d in draft.answer_dimension_ids:
            code = ab_code_from_label(d.label)
            out[code] = [o.name for o in d.option_line_ids if o.is_correct]
        return out

    # (a) generation derives the answer key + briefs from the plan.
    def test_a_generation_materializes_key_from_construction_keys(self):
        prompt = self._sop_prompt()
        payload = json.dumps([self._flaw_item()])
        with patch.object(vertex, "_call_vertex", return_value=payload):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        self.assertEqual(len(draft_ids), 1)
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(draft.question_type, "image_ab")
        self.assertTrue(draft.flaw_plan_json)
        stored = json.loads(draft.flaw_plan_json)
        keys = stored["construction_keys"]
        # the materialized answer key equals construction_keys exactly.
        self.assertEqual(self._draft_keys(draft),
                         {c: [v] for c, v in keys.items()})
        # OC still names the clean side after any random flip (never the flawed).
        clean = "Response A" if stored["flawed_side"] == "b" else "Response B"
        self.assertEqual(keys["OC"], clean)
        # clean/flawed prompts render to the correct slots.
        briefs = {b["slot"]: b["prompt"]
                  for b in json.loads(draft.image_brief_json)}
        clean_side = "a" if stored["flawed_side"] == "b" else "b"
        self.assertEqual(briefs[stored["flawed_side"]], stored["flawed_prompt"])
        self.assertEqual(briefs[clean_side], stored["clean_prompt"])
        self.assertEqual(draft.image_state, "pending")

    def _rendered_draft(self, dimensions_keys, plan):
        return self.Draft.create({
            "prompt_id": self._sop_prompt().id,
            "name": "AB flaw draft",
            "question_prompt": "Pick the correct render.",
            "question_type": "image_ab",
            "flaw_plan_json": json.dumps(plan),
            "dimensions_json": json.dumps(
                ab_specs_from_construction_keys(dimensions_keys)),
            "image_state": "rendered",
            "images_json": json.dumps([
                {"slot": "a", "label": "A", "data": "data:image/png;base64,AAAA"},
                {"slot": "b", "label": "B", "data": "data:image/png;base64,BBBB"},
            ]),
        })

    # (b) approve copies the plan and materializes a matching bank key.
    def test_b_approve_succeeds_and_copies_plan(self):
        plan = _plan("b")
        keys = plan["construction_keys"]
        draft = self._rendered_draft(keys, plan)
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        q = draft.approved_question_id
        self.assertTrue(q.flaw_plan_json)
        self.assertEqual(json.loads(q.flaw_plan_json)["construction_keys"], keys)
        materialized = {}
        for qd in q.question_dimension_ids:
            materialized[ab_code_from_label(qd.name)] = [
                ol.name for ol in qd.option_line_ids if ol.is_correct]
        self.assertEqual(materialized, {c: [v] for c, v in keys.items()})

    # (c) approve refuses when the materialized key drifted from the plan.
    def test_c_approve_raises_on_key_drift(self):
        plan = _plan("b")
        tampered = dict(plan["construction_keys"])
        tampered["OC"] = "Response B"  # flip OC away from the plan's clean side
        draft = self._rendered_draft(tampered, plan)
        with self.assertRaises(UserError):
            draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "draft")


class TestFlawInjectionScoreTime(_ScoringBase):
    _LABELS = {
        "IF": "Instruction Following (IF)", "VQ": "Visual Quality (VQ)",
        "LAI": "Less AI Generated (LAI)", "OC": "Overall Choice (OC)",
    }

    def _flaw_ab_question(self, name, correct_by_code, flaw_plan=None):
        q = self.Question.create({
            "name": name, "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "The clean render wins.",
            "flaw_plan_json": json.dumps(flaw_plan) if flaw_plan else False,
        })
        for code, label in self._LABELS.items():
            self._attach_dim(q, label, correct_by_code[code])
        return q

    def _pick_correct(self, q):
        return [
            (0, 0, {"question_dimension_id": qd.id,
                    "selected_option_id": qd.option_line_ids.filtered(
                        "is_correct")[:1].id})
            for qd in q.question_dimension_ids]

    # (d) tampered stored key vs its flaw plan -> raw 0 + integrity flag, no LLM.
    def test_d_score_time_key_drift_scores_zero_flagged(self):
        plan = _plan("b")
        tampered = dict(plan["construction_keys"])
        tampered["OC"] = "Response B"  # stored key no longer matches the plan
        q = self._flaw_ab_question("AB-drift", tampered, flaw_plan=plan)
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="",
                          lines=self._pick_correct(q))
        self.assertFalse(resp._image_ab_uses_llm())
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 0.0)
        stored = json.loads(resp.llm_result_json)
        self.assertIn("key_drift", stored["flags"])
        self.assertTrue(stored["integrity_key_drift"])
        self.assertEqual(resp.llm_gate, "key_drift")

    # (e) NULL flaw_plan_json: guards no-op, verdict lane scores as before.
    def test_e_null_plan_scores_normally_no_regression(self):
        correct = {"IF": "Response A", "VQ": "Response B",
                   "LAI": "Both Good", "OC": "Response A"}
        q = self._flaw_ab_question("AB-legacy", correct, flaw_plan=None)
        self.assertFalse(q.flaw_plan_json)
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="",
                          lines=self._pick_correct(q))
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)
        stored = json.loads(resp.llm_result_json)
        self.assertNotIn("key_drift", stored.get("flags") or [])
