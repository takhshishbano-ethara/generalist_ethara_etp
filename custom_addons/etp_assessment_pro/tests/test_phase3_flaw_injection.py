# -*- coding: utf-8 -*-
"""Phase 3 image_ab flaw-injection tests (fully mocked, offline).

image_ab generation plans ONE clean and ONE deliberately FLAWED image and stamps
a per-dimension answer key DERIVED from the plan's construction_keys, so the key
is ground-truth BY CONSTRUCTION. The plan is persisted as flaw_plan_json on the
draft and copied to the bank question on approve. Two drift guards hard-fail on
any divergence between the materialized answer key and construction_keys:

1. APPROVE guard (models/prompt.py _assert_no_key_drift): refuses to approve a
   draft whose materialized bank key != construction_keys.
2. SCORE-TIME guard (services/scoring.py _ab_key_drift / _store_ab_key_drift):
   a stored key that drifted from construction_keys scores raw 0 with an
   integrity flag, logging 'KEY DRIFT', through the same immutable store path.

Every guard no-ops on a NULL flaw_plan_json, so pre-Phase-3 image_ab questions
score exactly as before (Phase 2 two-lane). These tests run offline: any Vertex
call is mocked and asserted un-called on the guard paths.
"""
import base64
import json
from unittest.mock import patch

from odoo.exceptions import UserError

from odoo.addons.etp_assessment_pro.services import vertex, scoring
from odoo.addons.etp_assessment_pro import constants
from odoo.addons.etp_assessment_pro.constants import (
    ab_specs_from_construction_keys, ab_dimension_label, ab_code_from_label,
    validate_flaw_plan,
)

from .test_scoring_v6 import _ScoringBase


# A valid plan (flawed side b): no verdict names the flawed side (Response B) and
# OC names the clean side (Response A). Kept fixed so the derivation is
# deterministic in the direct-construct tests.
CONSTRUCTION_KEYS = {"IF": "Response A", "VQ": "Both Good",
                     "LAI": "Both Good", "OC": "Response A"}


def _flaw_plan(keys=None):
    return {
        "flawed_side": "b",
        "clean_prompt": "A clean photorealistic red car stopped at a 'Stop' sign.",
        "flawed_prompt": "The same scene but the sign misspells 'Stop' as 'Stpo'.",
        "injected_flaws": ["misspelled label 'Stpo'", "extra floating object"],
        "construction_keys": dict(keys or CONSTRUCTION_KEYS),
    }


def _bank_keys(question):
    """Map an approved bank question's dimensions back to {code: verdict} from
    the is_correct option lines."""
    out = {}
    for qd in question.question_dimension_ids:
        code = ab_code_from_label(qd.name)
        correct = qd.option_line_ids.filtered("is_correct").mapped("name")
        if code and correct:
            out[code] = correct[0]
    return out


class TestPhase3FlawInjectionGenerationApprove(_ScoringBase):
    """(a) generation materializes a key equal to construction_keys; (b) approve
    succeeds when the key matches; (c) approve raises on a forced key mismatch."""

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _sop_prompt(self):
        p = self.Prompt.create({"name": "SOP P3"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": p.id, "name": "sop.pdf",
            "file": base64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        return p

    def _draft_keys(self, draft):
        """Map a draft's derived dimension specs back to {code: verdict}."""
        out = {}
        for spec in draft._dimension_specs():
            code = ab_code_from_label(spec["label"])
            if code and spec["correct"]:
                out[code] = spec["correct"][0]
        return out

    def _rendered_ab_draft(self, dims_specs):
        """A ready-to-approve flaw-injected image_ab draft: a persisted plan, the
        given derived dimensions, and both A/B images so the image-ready approval
        guard is satisfied and only the key-drift guard is under test."""
        prompt = self.Prompt.create({"name": "P3 approve"})
        return self.Draft.create({
            "prompt_id": prompt.id, "name": "AB draft",
            "question_type": "image_ab",
            "flaw_plan_json": json.dumps(_flaw_plan()),
            "dimensions_json": json.dumps(dims_specs),
            "official_reasoning": "The clean side wins by construction.",
            "images_json": json.dumps([
                {"slot": "a", "label": "A", "data": "data:image/png;base64,AAAA"},
                {"slot": "b", "label": "B", "data": "data:image/png;base64,BBBB"}]),
            "image_state": "rendered",
        })

    # (a) A generated image_ab draft carrying a flaw plan materializes a
    #     dimension answer key EQUAL to its construction_keys.
    def test_a_generated_draft_key_equals_construction_keys(self):
        prompt = self._sop_prompt()
        item = {
            "name": "AB flaw", "prompt": "Which image better follows the brief?",
            "question_type": "image_ab", "difficulty": "medium",
            "image_specs": {"flaw_plan": _flaw_plan()},
        }
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([item])):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        self.assertEqual(len(draft_ids), 1)
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(draft.question_type, "image_ab")
        # A plan was persisted (new-question-only flaw injection).
        self.assertTrue(draft.flaw_plan_json)
        stored = json.loads(draft.flaw_plan_json)
        keys = {k.upper(): v for k, v in stored["construction_keys"].items()}
        # The answer key is DERIVED from construction_keys -> exactly equal,
        # regardless of the random clean/flawed slot assignment.
        self.assertEqual(self._draft_keys(draft), keys)
        self.assertEqual(set(keys), {"IF", "VQ", "LAI", "OC"})
        # The persisted (possibly slot-flipped) plan is still internally valid:
        # no verdict names the flawed side and OC names the clean side.
        self.assertEqual(validate_flaw_plan(stored), [])

    # (b) Approve SUCCEEDS when the materialized key matches construction_keys;
    #     the plan is copied to the bank question.
    def test_b_approve_succeeds_when_key_matches(self):
        specs = ab_specs_from_construction_keys(CONSTRUCTION_KEYS)
        draft = self._rendered_ab_draft(specs)
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        q = draft.approved_question_id
        self.assertTrue(q)
        # flaw_plan_json copied verbatim to the bank question.
        self.assertTrue(q.flaw_plan_json)
        self.assertEqual(
            json.loads(q.flaw_plan_json)["construction_keys"], CONSTRUCTION_KEYS)
        # The materialized bank key equals construction_keys (no drift).
        self.assertEqual(_bank_keys(q), CONSTRUCTION_KEYS)

    # (c) Approve RAISES UserError when the stored key is force-mismatched vs
    #     construction_keys (OC flipped away from its construction key).
    def test_c_approve_raises_on_forced_key_drift(self):
        specs = ab_specs_from_construction_keys(CONSTRUCTION_KEYS)
        for spec in specs:
            if ab_code_from_label(spec["label"]) == "OC":
                # construction key is 'Response A'; force the stored key to B.
                spec["correct"] = ["Response B"]
        draft = self._rendered_ab_draft(specs)
        with self.assertRaises(UserError):
            draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "draft")          # not approved


class TestPhase3ScoreTimeDrift(_ScoringBase):
    """(d) a tampered stored key scores raw 0 with an integrity flag; (e) a NULL
    flaw_plan_json image_ab scores normally (no regression)."""

    def _flaw_bank_q(self, name, keys, tamper=None):
        """A bank image_ab with a persisted flaw plan and four AB dimensions;
        ``tamper`` overrides the is_correct verdict for a code so the stored key
        diverges from construction_keys."""
        q = self.Question.create({
            "name": name, "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "The clean side wins by construction.",
            "flaw_plan_json": json.dumps(_flaw_plan(keys)),
        })
        for code in ("IF", "VQ", "LAI", "OC"):
            correct = (tamper or {}).get(code, keys[code])
            self._attach_dim(q, ab_dimension_label(code), correct)
        return q

    def _pick_correct(self, q):
        """Candidate lines choosing each dimension's stored-correct option."""
        lines = []
        for qd in q.question_dimension_ids:
            correct = qd.option_line_ids.filtered("is_correct")[:1]
            if correct:
                lines.append((0, 0, {
                    "question_dimension_id": qd.id,
                    "selected_option_id": correct.id}))
        return lines

    # (d) A question whose stored key was tampered vs its flaw_plan_json scores
    #     raw 0 with the integrity flag, and Vertex is never called.
    def test_d_tampered_key_scores_zero_with_integrity_flag(self):
        q = self._flaw_bank_q("AB drift", CONSTRUCTION_KEYS,
                              tamper={"OC": "Response B"})  # key is Response A
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="",
                          lines=self._pick_correct(q))
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()                            # guard short-circuits
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 0.0)    # raw 0
        self.assertEqual(resp.llm_gate, "key_drift")
        flags = json.loads(resp.llm_flags_json or "[]")
        self.assertIn("key_drift", flags)
        stored = json.loads(resp.llm_result_json)
        self.assertTrue(stored.get("integrity_key_drift"))

    # (e) An existing image_ab with a NULL flaw plan is completely unaffected by
    #     the guard: it scores on the Phase 2 verdict lane exactly as before.
    def test_e_null_flaw_plan_scores_normally_no_regression(self):
        q = self.Question.create({
            "name": "AB legacy", "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction."})
        self.assertFalse(q.flaw_plan_json)
        axes = [
            ("Instruction Following (IF)", "Response A"),
            ("Visual Quality (VQ)", "Response B"),
            ("Label Accuracy (LAI)", "Both Good"),
            ("Overall Choice (OC)", "Response A"),
        ]
        for name, correct in axes:
            self._attach_dim(q, name, correct)
        pick = dict(axes)
        ev, app, ass = self._evaluator()
        lines = [(0, 0, {"question_dimension_id": qd.id,
                         "selected_option_id": self._opt(qd, pick[qd.name]).id})
                 for qd in q.question_dimension_ids]
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()                            # verdict-only, no LLM
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)  # 4/4 verdicts correct
        self.assertNotEqual(resp.llm_gate, "key_drift")
        stored = json.loads(resp.llm_result_json)
        self.assertFalse(stored.get("integrity_key_drift"))
