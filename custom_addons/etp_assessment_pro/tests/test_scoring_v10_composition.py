# -*- coding: utf-8 -*-
"""Regression tests: the v10 judge payload must not destroy the composed score.

WHY THIS FILE EXISTS
--------------------
Every pre-existing image_ab/image_label test mocks a *v6-shaped* judge reply: a
0..1 ``score`` and no ``golden_claims``. Production sends the **v10** shape --
``prompts/scoring.md`` mandates "Every score runs 0 to 100" (:25) and makes
``golden_claims`` mandatory on every ungated keyed entry (:478). On that real
payload two bugs fired that no fixture could see:

1. ``_blend_ab_justification`` did ``max(0.0, min(1.0, score))`` on a 0-100
   number, so a judge score of 91 clamped to 1.0 -- **full justification credit,
   always**.
2. ``_store_scored`` then ran ``_recompute_v10`` and overwrote the composed
   blend with the justification-only score, so the 0.75-weighted A/B verdict --
   whether the candidate picked the right image, the entire point of the type --
   **contributed nothing to the stored mark**. A candidate who picked the wrong
   image on every axis but wrote fluent prose scored full marks.

Both are offline-provable: no Vertex call, no LLM budget. If these fail, the
stored mark is not the mark the audit trail asserts.
"""
import json
from unittest.mock import patch

from odoo.addons.etp_assessment_pro.services import vertex, scoring

from .test_scoring_v6 import _ScoringBase


def _claims(*verdicts):
    """A v10 golden_claims list: first claim deciding, rest supporting.

    Shape per prompts/scoring.md:309-317.
    """
    out = []
    for i, verdict in enumerate(verdicts):
        claim = {"claim": "claim %d" % i, "verdict": verdict}
        if i == 0:
            claim["tag"] = "deciding"
        out.append(claim)
    return out


class TestV10CompositionBase(_ScoringBase):
    def _ab_question(self, name, axes):
        q = self.Question.create({
            "name": name,
            "prompt": "Compare A and B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction.",
        })
        for axis_name, correct in axes:
            self._attach_dim(q, axis_name, correct)
        return q

    def _lines(self, q, picks):
        return [
            (0, 0, {"question_dimension_id": qd.id,
                    "selected_option_id": self._opt(qd, picks[qd.name]).id})
            for qd in q.question_dimension_ids
        ]

    def _v10_item(self, resp, score, claims, **kw):
        """A judge reply in the shape production actually sends."""
        item = {
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "score": score,                 # v10: 0-100, NOT 0-1
            "golden_claims": claims,        # v10: mandatory on keyed entries
            "rubric_source": "generated", "rubric": {},
            "gate": "none", "reference_answer": "A...",
            "reasoning": "r", "verdict_consistency": "match",
            "feedback": "f", "flags": [],
        }
        item.update(kw)
        return item


class TestABVerdictSurvivesRecompute(TestV10CompositionBase):
    """The A/B verdict must reach llm_raw_100 on the real v10 payload."""

    AXES = [
        ("Instruction Following (IF)", "Response A"),
        ("Overall Choice (OC)", "Response B"),
    ]

    def test_wrong_image_every_axis_plus_fluent_prose_is_not_full_marks(self):
        """THE bug: wrong on every verdict + a perfect justification != 100.

        verdict=0.0, justification=100 -> 0.75*0 + 0.25*1.0 = 0.25 -> raw 25.
        Before the fix _recompute_v10 discarded the blend and stored ~100.
        """
        q = self._ab_question("AB-v10-wrong", self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        # Invert every pick: candidate is wrong on all axes.
        inverted = {"Instruction Following (IF)": "Response B",
                    "Overall Choice (OC)": "Response A"}
        resp = self._resp(ev, app, ass, q, justification="Eloquent but wrong.",
                          lines=self._lines(q, inverted))
        self.assertTrue(resp._image_ab_uses_llm())
        # All claims hit -> recompute yields 100 for the justification lane.
        self._mock_score(resp, [self._v10_item(
            resp, 100, _claims("hit", "hit"))])
        resp.invalidate_recordset()

        self.assertAlmostEqual(resp.llm_raw_100, 25.0)
        stored = json.loads(resp.llm_result_json)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 0.0)
        self.assertAlmostEqual(stored["ab_scores"]["justification_score"], 1.0)
        self.assertAlmostEqual(stored["ab_scores"]["blend"], 0.25)

    def test_right_image_every_axis_plus_weak_prose_keeps_verdict_credit(self):
        """verdict=1.0, justification=20 -> 0.75 + 0.05 = 0.80 -> raw 80."""
        q = self._ab_question("AB-v10-right", self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="Thin.",
                          lines=self._lines(q, dict(self.AXES)))
        # deciding=miss(0), supporting=miss(0) -> 0; then a 20 emitted score is
        # ignored in favour of the re-derived verdicts. Use claims worth 20:
        # deciding partial(50) + supporting miss(0) -> 0.7*50 + 0.3*0 = 35.
        self._mock_score(resp, [self._v10_item(
            resp, 35, _claims("partial", "miss"))])
        resp.invalidate_recordset()

        stored = json.loads(resp.llm_result_json)
        self.assertAlmostEqual(stored["ab_scores"]["verdict_score"], 1.0)
        # justification lane = re-derived 35/100.
        self.assertAlmostEqual(stored["ab_scores"]["justification_score"], 0.35)
        self.assertAlmostEqual(resp.llm_raw_100, 75.0 + 0.25 * 35.0)

    def test_judge_score_is_not_clamped_to_full_credit(self):
        """A 0-100 judge score must not become 1.0 (=100%) via min(1.0, x)."""
        q = self._ab_question("AB-v10-clamp", self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        # Wrong on every axis so any justification inflation is visible.
        inverted = {"Instruction Following (IF)": "Response B",
                    "Overall Choice (OC)": "Response A"}
        resp = self._resp(ev, app, ass, q, justification="Mediocre.",
                          lines=self._lines(q, inverted))
        # No golden_claims -> falls back to the emitted score, which is 50/100.
        item = self._v10_item(resp, 50, None)
        item.pop("golden_claims")
        self._mock_score(resp, [item])
        resp.invalidate_recordset()

        stored = json.loads(resp.llm_result_json)
        # 50 must read as 0.5, not clamp to 1.0.
        self.assertAlmostEqual(stored["ab_scores"]["justification_score"], 0.5)
        self.assertAlmostEqual(resp.llm_raw_100, 12.5)

    def test_legacy_0_1_score_still_resolves(self):
        """Back-compat: a v6-shaped 0..1 score keeps its old meaning."""
        q = self._ab_question("AB-legacy", self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="ok",
                          lines=self._lines(q, dict(self.AXES)))
        item = self._v10_item(resp, 0.20, None)
        item.pop("golden_claims")
        self._mock_score(resp, [item])
        resp.invalidate_recordset()
        # 0.75*1.0 + 0.25*0.2 = 0.80 -> raw 80 (unchanged from Phase 2).
        self.assertAlmostEqual(resp.llm_raw_100, 80.0)

    def test_drift_note_does_not_fire_on_every_answer(self):
        """The drift check must compare like scales.

        It compared a 0-1 blend against a 0-100 recompute, so |0.85-85| > 1.5
        flagged score_recomputed on EVERY answer, destroying the signal.
        """
        q = self._ab_question("AB-drift", self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="Solid.",
                          lines=self._lines(q, dict(self.AXES)))
        # Judge emits 100 and its claims re-derive to 100 -> no drift.
        self._mock_score(resp, [self._v10_item(
            resp, 100, _claims("hit", "hit"))])
        resp.invalidate_recordset()
        flags = json.loads(resp.llm_flags_json or "[]")
        self.assertNotIn("score_recomputed", flags)
        self.assertNotIn("needs_review", flags)
        self.assertAlmostEqual(resp.llm_raw_100, 100.0)


class TestScoringSeesTheMedia(TestV10CompositionBase):
    """prompts/scoring.md promises "the rendered media is attached to the call
    when available". It was not: _score_submission called _call_vertex with no
    user_parts, so every image_ab / image_label justification was graded blind
    on its text while the judge was asked to reason about images it never saw.
    """

    AXES = [("Overall Choice (OC)", "Response A")]

    # 1x1 transparent PNG.
    PNG = (b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           b"YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def _ab_with_images(self, name):
        q = self._ab_question(name, self.AXES)
        for slot in ("a", "b"):
            self.env["etp.assessment.pro.question.image"].create({
                "question_id": q.id, "slot": slot, "image": self.PNG,
                "label": "Response %s" % slot.upper(),
            })
        return q

    def _capture_parts(self, resp, item):
        """Run the scorer and return the user_parts actually sent to Vertex."""
        captured = {}

        def _fake(env, system_prompt, user_text, **kw):
            captured["parts"] = kw.get("user_parts")
            captured["text"] = user_text
            return json.dumps({
                "judge_model": "test", "pass_threshold": 70,
                "results": [item],
            })

        with patch.object(vertex, "_call_vertex", side_effect=_fake):
            scoring._score_submission(self.env, resp)
        return captured

    def test_image_ab_attaches_both_rendered_images(self):
        q = self._ab_with_images("AB-media")
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="A is sharper.",
                          lines=self._lines(q, dict(self.AXES)))
        item = self._v10_item(resp, 80, _claims("hit"))
        cap = self._capture_parts(resp, item)

        parts = cap.get("parts") or []
        inline = [p for p in parts if "inlineData" in p]
        self.assertEqual(
            len(inline), 2,
            "image_ab must send BOTH rendered images; the judge cannot compare "
            "A and B it has never seen")
        self.assertEqual(parts[0].get("text"), cap["text"])
        for p in inline:
            self.assertEqual(p["inlineData"]["mimeType"], "image/png")
            self.assertTrue(p["inlineData"]["data"])
        self.assertIn("ATTACHED MEDIA", cap["text"])

    def test_missing_media_is_declared_not_silently_omitted(self):
        """No images -> the judge must be TOLD, so it can stamp media_unseen
        instead of inventing what the image showed."""
        q = self._ab_question("AB-nomedia", self.AXES)  # no images created
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="A is sharper.",
                          lines=self._lines(q, dict(self.AXES)))
        item = self._v10_item(resp, 80, _claims("hit"))
        cap = self._capture_parts(resp, item)

        inline = [p for p in (cap.get("parts") or []) if "inlineData" in p]
        self.assertEqual(len(inline), 0)
        self.assertIn("NO MEDIA AVAILABLE", cap["text"])
        self.assertIn("media_unseen", cap["text"])

    def test_directive_matches_the_v10_system_prompt(self):
        """The runtime directive said 'subjective-judge-v6 ... schema_version'
        while the system prompt is v10 and forbids schema_version."""
        q = self._ab_with_images("AB-directive")
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="ok",
                          lines=self._lines(q, dict(self.AXES)))
        item = self._v10_item(resp, 80, _claims("hit"))
        cap = self._capture_parts(resp, item)

        self.assertNotIn("subjective-judge-v6", cap["text"])
        self.assertNotIn("schema_version", cap["text"])
        for key in ("judge_model", "pass_threshold", "results"):
            self.assertIn(key, cap["text"])

    def test_text_only_type_attaches_no_media(self):
        """subjective_rubric must not pay for image parts."""
        q = self.Question.create({
            "name": "Text only", "prompt": "Explain.",
            "question_type": "subjective_rubric",
        })
        ev, app, ass = self._evaluator()
        resp = self._resp(ev, app, ass, q, justification="Because.")
        item = self._v10_item(resp, 80, _claims("hit"))
        cap = self._capture_parts(resp, item)
        inline = [p for p in (cap.get("parts") or []) if "inlineData" in p]
        self.assertEqual(len(inline), 0)
        self.assertNotIn("NO MEDIA AVAILABLE", cap["text"])


class TestGateVocabularyMatchesThePrompt(TestV10CompositionBase):
    """prompts/scoring.md emits 'unscorable:wrong_item'; the code tested for a
    bare 'wrong_item' and never matched, so the strongest cheating signal
    available raised no integrity alert.

    Note there are TWO live gate vocabularies (see constants.py): the platform's
    own pre-LLM gates (services/gates.py -> 'empty_answer') and the judge's
    ('unscorable:empty'). Neither is stale; llm_gate must keep whichever its
    producer emitted, so a reviewer can tell which one fired.
    """

    AXES = [("Overall Choice (OC)", "Response A")]

    def _gated(self, gate):
        q = self._ab_question("AB-gate-%s" % gate.replace(":", "-"), self.AXES)
        ev, app, ass = self._evaluator()
        ass.require_justification_image_comparison = True
        resp = self._resp(ev, app, ass, q, justification="answer to a different question",
                          lines=self._lines(q, dict(self.AXES)))
        item = self._v10_item(resp, 0, None, gate=gate)
        item.pop("golden_claims")
        self._mock_score(resp, [item])
        resp.invalidate_recordset()
        return resp

    def test_unscorable_wrong_item_raises_integrity_alert(self):
        resp = self._gated("unscorable:wrong_item")
        self.assertEqual(resp.llm_gate, "unscorable:wrong_item")
        self.assertIn("integrity_alert", json.loads(resp.llm_flags_json or "[]"))

    def test_injection_attempt_raises_integrity_alert(self):
        resp = self._gated("injection_attempt")
        self.assertIn("integrity_alert", json.loads(resp.llm_flags_json or "[]"))

    def test_legacy_bare_wrong_item_still_alerts(self):
        resp = self._gated("wrong_item")
        self.assertIn("integrity_alert", json.loads(resp.llm_flags_json or "[]"))

    def test_platform_empty_answer_gate_is_stored_verbatim(self):
        """The platform's own gate vocabulary must not be rewritten into the
        judge's: services/gates.py emits 'empty_answer' and that is what the
        audit trail has to show."""
        resp = self._gated("empty_answer")
        self.assertEqual(resp.llm_gate, "empty_answer")

    def test_benign_gate_does_not_alert(self):
        resp = self._gated("none")
        self.assertNotIn(
            "integrity_alert", json.loads(resp.llm_flags_json or "[]"))
