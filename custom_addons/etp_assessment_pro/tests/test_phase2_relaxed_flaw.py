# -*- coding: utf-8 -*-
"""Phase 2: RELAX the image_ab flaw invariant to the reference per-dimension model.

Phase 1 kept the STRICT invariant (exactly one clean + one flawed side, no verdict
names the flawed side, OC names the clean side). Phase 2 relaxes it to the
reference's richer model while keeping the one hard guarantee (OC is always
DECIDED to a single side). What is enforced stays mechanically checkable; the
semantic flaw->dimension mapping is deferred to the Phase-3 verification loop.

Covered (all offline, every Vertex call mocked):
  (a) a per-dimension plan where a FLAWED side WINS a different dimension -> valid
      draft, answer key materialized == construction_keys (was rejected before);
  (b) a BOTH-flawed plan with a "Both Bad" dimension + OC decided -> valid draft;
  (c) OC = "Both Good"/"Both Bad" -> REJECTED (OC must be decided);
  (d) scoring: a "Both Bad" keyed dimension scores a candidate's "Both Bad" pick
      as correct and any other pick as wrong (deterministic exact match);
  (e) back-compat: the strict/legacy Phase-1 plans still validate and score.
"""
import base64
import json
from unittest.mock import patch

from odoo.addons.etp_assessment_pro.services import vertex, scoring
from odoo.addons.etp_assessment_pro.constants import (
    validate_flaw_plan, ab_dimension_label, ab_code_from_label,
)

from .test_scoring_v6 import _ScoringBase


def _single_flawed_win_plan():
    """NEW-shape, ONE flawed side (b), but a dimension (IF) names that flawed
    side — legal now: side b's LAI/VQ flaw is worse, yet it follows the
    instruction better, so it wins IF. Strict Phase-1 rejected this."""
    return {
        "faithful_side": "a",
        "worker_prompt": "A single blue bicycle leaning on a brick wall, "
                         "photorealistic.",
        "render_prompts": {
            "a": "A blue bicycle leaning on a brick wall, but the frame is "
                 "slightly blurred and low-detail.",
            "b": "A blue bicycle leaning on a brick wall, tack-sharp, but with a "
                 "second faint ghost wheel behind it.",
        },
        "planted": {"a": ["soft/blurred frame (VQ)"],
                    "b": ["faint ghost extra wheel (LAI)"]},
        "construction_keys": {"IF": "Response B", "VQ": "Response B",
                              "LAI": "Response A", "OC": "Response A"},
    }


def _both_flawed_plan(keys=None):
    """NEW-shape BOTH-flawed pair (faithful_side null): each side carries a
    planted flaw, VQ is 'Both Bad', OC is still decided to a single side."""
    return {
        "faithful_side": None,
        "worker_prompt": "A red stop sign at a crossroads reading 'STOP', "
                         "photorealistic.",
        "render_prompts": {
            "a": "A red octagonal sign reading 'STOP' but tilted and slightly "
                 "pixelated.",
            "b": "A red octagonal sign misspelling it as 'STPO', with clean sharp "
                 "rendering.",
        },
        "planted": {"a": ["pixelated / low VQ"],
                    "b": ["misspelled 'STPO' (IF) and also low VQ noise"]},
        "construction_keys": keys or {
            "IF": "Response A", "VQ": "Both Bad",
            "LAI": "Both Good", "OC": "Response A"},
    }


def _legacy_plan():
    """OLD clean/flawed shape (still accepted, mapped in) with the strict-style
    key (no verdict names the flawed side, OC names the clean side)."""
    return {
        "flawed_side": "b",
        "clean_prompt": "A clean photorealistic red car stopped at a 'Stop' sign.",
        "flawed_prompt": "The same scene but the sign misspells 'Stop' as 'Stpo'.",
        "injected_flaws": ["misspelled label 'Stpo'"],
        "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                              "LAI": "Both Good", "OC": "Response A"},
    }


class TestPhase2RelaxedValidation(_ScoringBase):
    """Pure-function validation of the relaxed invariant."""

    def test_a_flawed_side_may_win_a_different_dimension(self):
        self.assertEqual(validate_flaw_plan(_single_flawed_win_plan()), [])

    def test_b_both_flawed_with_both_bad_is_valid(self):
        self.assertEqual(validate_flaw_plan(_both_flawed_plan()), [])

    def test_c_oc_both_good_is_rejected(self):
        bad = _both_flawed_plan({"IF": "Response A", "VQ": "Both Bad",
                                 "LAI": "Both Good", "OC": "Both Good"})
        errs = validate_flaw_plan(bad)
        self.assertTrue(any("OC" in e and "DECIDED" in e for e in errs), errs)

    def test_c_oc_both_bad_is_rejected(self):
        bad = _both_flawed_plan({"IF": "Response A", "VQ": "Both Bad",
                                 "LAI": "Both Good", "OC": "Both Bad"})
        errs = validate_flaw_plan(bad)
        self.assertTrue(any("OC" in e and "DECIDED" in e for e in errs), errs)

    def test_both_bad_requires_both_sides_flawed(self):
        # A single-clean-side plan cannot carry a 'Both Bad' dimension.
        bad = _single_flawed_win_plan()
        bad["construction_keys"]["VQ"] = "Both Bad"
        errs = validate_flaw_plan(bad)
        self.assertTrue(any("Both Bad" in e for e in errs), errs)

    def test_e_legacy_plan_still_validates(self):
        self.assertEqual(validate_flaw_plan(_legacy_plan()), [])


class TestPhase2RelaxedGeneration(_ScoringBase):
    """Generation materializes a ground-truth key for the relaxed plans."""

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _sop_prompt(self):
        p = self.Prompt.create({"name": "SOP P2"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": p.id, "name": "sop.pdf",
            "file": base64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        return p

    def _draft_keys(self, draft):
        out = {}
        for spec in draft._dimension_specs():
            code = ab_code_from_label(spec["label"])
            if code and spec["correct"]:
                out[code] = spec["correct"][0]
        return out

    def _gen_one(self, plan):
        item = {"name": "AB flaw", "prompt": "Which image better follows the brief?",
                "question_type": "image_ab", "difficulty": "medium",
                "image_specs": {"flaw_plan": plan}}
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([item])):
            draft_ids = vertex.generate_questions_from_sop(self.env,
                                                           self._sop_prompt())
        self.assertEqual(len(draft_ids), 1,
                         "relaxed image_ab item must NOT be dropped as malformed")
        return self.Draft.browse(draft_ids)

    # (a) A flawed side winning a different dimension -> valid draft, key derived
    #     from construction_keys (== the persisted, possibly slot-flipped, keys).
    def test_a_flawed_side_win_materializes_key(self):
        draft = self._gen_one(_single_flawed_win_plan())
        self.assertEqual(draft.question_type, "image_ab")
        self.assertTrue(draft.flaw_plan_json)
        stored = json.loads(draft.flaw_plan_json)
        self.assertEqual(validate_flaw_plan(stored), [])
        keys = {k.upper(): v for k, v in stored["construction_keys"].items()}
        self.assertEqual(self._draft_keys(draft), keys)
        self.assertEqual(set(keys), {"IF", "VQ", "LAI", "OC"})

    # (b) A both-flawed plan with a Both Bad dimension + OC decided -> valid draft;
    #     the persisted plan keeps faithful_side both-flawed and OC decided.
    def test_b_both_flawed_both_bad_materializes_key(self):
        draft = self._gen_one(_both_flawed_plan())
        stored = json.loads(draft.flaw_plan_json)
        self.assertEqual(validate_flaw_plan(stored), [])
        keys = {k.upper(): v for k, v in stored["construction_keys"].items()}
        self.assertEqual(self._draft_keys(draft), keys)
        # A Both Bad key survived into the materialized answer key.
        self.assertIn("Both Bad", keys.values())
        # OC stayed decided to a single side after any random slot flip.
        self.assertIn(keys["OC"], ("Response A", "Response B"))
        # Both sides carried a planted flaw (both-flawed persisted plan).
        self.assertTrue(stored["planted"]["a"] and stored["planted"]["b"])


class TestPhase2RelaxedScoring(_ScoringBase):
    """Deterministic verdict scoring against a 'Both Bad' keyed dimension."""

    def _flaw_bank_q(self, name, keys):
        q = self.Question.create({
            "name": name, "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "Both sides flawed; OC decided by tiebreak.",
            "flaw_plan_json": json.dumps(_both_flawed_plan(keys)),
        })
        for code in ("IF", "VQ", "LAI", "OC"):
            self._attach_dim(q, ab_dimension_label(code), keys[code])
        return q

    def _pick(self, q, overrides=None):
        """Candidate lines: pick each dimension's stored-correct option, except
        where ``overrides`` (code->option name) forces a different pick."""
        overrides = overrides or {}
        lines = []
        for qd in q.question_dimension_ids:
            code = ab_code_from_label(qd.name)
            want = overrides.get(code)
            opt = (self._opt(qd, want) if want
                   else qd.option_line_ids.filtered("is_correct")[:1])
            if opt:
                lines.append((0, 0, {"question_dimension_id": qd.id,
                                     "selected_option_id": opt.id}))
        return lines

    KEYS = {"IF": "Response A", "VQ": "Both Bad",
            "LAI": "Both Good", "OC": "Response A"}

    # (d) Picking 'Both Bad' on the Both-Bad-keyed dimension is scored correct;
    #     all four correct -> raw 100. Vertex is never called (verdict lane only).
    def test_d_both_bad_pick_correct_scores_full(self):
        q = self._flaw_bank_q("AB both-bad correct", self.KEYS)
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="",
                          lines=self._pick(q))
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)
        self.assertNotEqual(resp.llm_gate, "key_drift")

    # (d) Picking 'Response A' on the Both-Bad-keyed VQ is scored wrong -> 3/4.
    def test_d_wrong_pick_on_both_bad_dim_loses_that_dim(self):
        q = self._flaw_bank_q("AB both-bad wrong", self.KEYS)
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="",
                          lines=self._pick(q, overrides={"VQ": "Response A"}))
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertAlmostEqual(resp.llm_raw_100, 75.0)

    # (e) A strict/legacy plan still scores on the verdict lane exactly as before.
    def test_e_legacy_plan_scores_on_verdict_lane(self):
        keys = {"IF": "Response A", "VQ": "Both Good",
                "LAI": "Both Good", "OC": "Response A"}
        q = self.Question.create({
            "name": "AB legacy score", "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "Clean side wins.",
            "flaw_plan_json": json.dumps(_legacy_plan()),
        })
        for code in ("IF", "VQ", "LAI", "OC"):
            self._attach_dim(q, ab_dimension_label(code), keys[code])
        ev, app, ass = self._evaluator()
        lines = [(0, 0, {"question_dimension_id": qd.id,
                         "selected_option_id":
                             qd.option_line_ids.filtered("is_correct")[:1].id})
                 for qd in q.question_dimension_ids]
        resp = self._resp(ev, app, ass, q, justification="", lines=lines)
        with patch.object(vertex, "_call_vertex") as m:
            scoring._score_submission(self.env, resp)
        m.assert_not_called()
        resp.invalidate_recordset()
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)
        self.assertNotEqual(resp.llm_gate, "key_drift")
