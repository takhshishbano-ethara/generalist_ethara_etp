# -*- coding: utf-8 -*-
"""Phase 1: the 3-prompt image_ab flaw_plan restructure + literal-render directive.

The flaw_plan now separates the candidate-facing TARGET prompt (worker_prompt)
from the two per-side RENDER prompts (render_prompts.a/.b), with planted flaws
per side. The OLD clean/flawed shape is still accepted and mapped in. Strict
flaw validation is UNCHANGED this phase. The render request text carries a
literal-rendering directive so the image model reproduces flaws/misspellings
verbatim instead of "fixing" them. All Vertex calls are mocked (offline).
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etp_assessment_pro.services import vertex
from odoo.addons.etp_assessment_pro.constants import (
    validate_flaw_plan, normalize_flaw_plan, ab_code_from_label,
)


def _new_plan():
    """A valid NEW-shape plan (faithful side a): OC names the faithful side and
    no verdict names the flawed side (Response B). worker_prompt is distinct from
    both render prompts."""
    return {
        "faithful_side": "a",
        "worker_prompt": "A red ceramic mug with the word 'Coffee' on a wooden table.",
        "render_prompts": {
            "a": "A red ceramic mug reading 'Coffee' on a plain wooden table, "
                 "single handle, photorealistic.",
            "b": "A red ceramic mug reading 'Coffe' with a second floating handle "
                 "above it, on a wooden table.",
        },
        "planted": {"a": [], "b": ["misspelled label 'Coffe'", "extra floating handle"]},
        "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                              "LAI": "Both Good", "OC": "Response A"},
    }


def _old_plan():
    """A valid OLD-shape plan (flawed side b) for the back-compat path."""
    return {
        "flawed_side": "b",
        "clean_prompt": "A clean photorealistic red car stopped at a 'Stop' sign.",
        "flawed_prompt": "The same scene but the sign misspells 'Stop' as 'Stpo'.",
        "injected_flaws": ["misspelled label 'Stpo'"],
        "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                              "LAI": "Both Good", "OC": "Response A"},
    }


def _item(plan, prompt="Which of the two images is the better response?"):
    return {
        "name": "AB flaw", "prompt": prompt, "question_type": "image_ab",
        "difficulty": "medium", "image_specs": {"flaw_plan": plan},
    }


@tagged("-at_install", "post_install")
class TestPhase1ThreePromptShape(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _sop_prompt(self):
        return self.Prompt.create({
            "name": "SOP P1",
            "source_text": "Author image A/B comparison questions."})

    def _draft_keys(self, draft):
        out = {}
        for spec in draft._dimension_specs():
            code = ab_code_from_label(spec["label"])
            if code and spec["correct"]:
                out[code] = spec["correct"][0]
        return out

    def _gen_one(self, plan, prompt="Which of the two images is the better response?"):
        sop = self._sop_prompt()
        with patch.object(vertex, "_call_vertex",
                          return_value=json.dumps([_item(plan, prompt)])):
            draft_ids = vertex.generate_questions_from_sop(self.env, sop)
        self.assertEqual(len(draft_ids), 1,
                         "the image_ab item must NOT be dropped as malformed")
        return self.Draft.browse(draft_ids)

    # (a) NEW shape -> valid draft; candidate prompt == worker_prompt; the two
    #     images are rendered from render_prompts.a/.b (distinct from the worker
    #     prompt); the key is derived from construction_keys.
    def test_a_new_shape_worker_render_split(self):
        plan = _new_plan()
        draft = self._gen_one(plan)
        self.assertEqual(draft.question_type, "image_ab")
        self.assertTrue(draft.flaw_plan_json)
        stored = json.loads(draft.flaw_plan_json)
        self.assertEqual(validate_flaw_plan(stored), [])

        # Candidate-facing question prompt is the worker/target prompt.
        self.assertEqual(draft.question_prompt, plan["worker_prompt"])

        # The two rendered briefs come from render_prompts, NOT the worker prompt.
        briefs = json.loads(draft.image_brief_json)
        brief_prompts = {b["prompt"] for b in briefs}
        self.assertEqual(
            brief_prompts,
            {plan["render_prompts"]["a"], plan["render_prompts"]["b"]})
        self.assertNotIn(plan["worker_prompt"], brief_prompts)

        # The answer key is DERIVED from construction_keys (== the persisted,
        # possibly slot-flipped, keys) and covers all four AB dimensions.
        keys = {k.upper(): v for k, v in stored["construction_keys"].items()}
        self.assertEqual(self._draft_keys(draft), keys)
        self.assertEqual(set(keys), {"IF", "VQ", "LAI", "OC"})

    # (b) OLD shape still works and is normalized to the new shape on persist.
    def test_b_old_shape_back_compat(self):
        plan = _old_plan()
        draft = self._gen_one(plan)
        self.assertTrue(draft.flaw_plan_json)
        stored = json.loads(draft.flaw_plan_json)
        self.assertEqual(validate_flaw_plan(stored), [])
        # Old clean_prompt becomes the worker/target prompt shown to the candidate.
        self.assertEqual(draft.question_prompt, plan["clean_prompt"])
        # Persisted plan carries the new 3-prompt fields.
        self.assertIn("render_prompts", stored)
        self.assertTrue(stored["render_prompts"]["a"])
        self.assertTrue(stored["render_prompts"]["b"])
        # The flawed side's planted list is non-empty; faithful side empty.
        flawed = stored["flawed_side"]
        faithful = stored["faithful_side"]
        self.assertTrue(stored["planted"][flawed])
        self.assertFalse(stored["planted"][faithful])

    # (c) The render request text carries the literal-rendering directive so the
    #     image model reproduces flaws/misspellings verbatim.
    def test_c_render_request_has_literal_directive(self):
        captured = {}

        def fake_gen_image(env, prompt, **kw):
            captured["prompt"] = prompt
            return ("QUJD", "image/png")

        briefs = [{"slot": "a", "label": "A",
                   "prompt": "A sign that misspells 'Stop' as 'Stpo'."}]
        with patch.object(vertex, "generate_image", side_effect=fake_gen_image):
            imgs = vertex.render_draft_images(self.env, briefs)
        self.assertEqual(len(imgs), 1)
        self.assertIn("Render the description literally", captured["prompt"])
        # The scene itself is still passed through verbatim.
        self.assertIn("Stpo", captured["prompt"])


class TestPhase1FlawPlanNormalization(TransactionCase):
    """Pure-function coverage of the old<->new normalization + strict validation
    (unchanged strictness this phase)."""

    def test_new_shape_normalizes_and_validates(self):
        norm = normalize_flaw_plan(_new_plan())
        self.assertEqual(norm["faithful_side"], "a")
        self.assertEqual(norm["flawed_side"], "b")
        self.assertTrue(norm["worker_prompt"])
        self.assertTrue(norm["render_prompts"]["a"])
        self.assertTrue(norm["render_prompts"]["b"])
        self.assertTrue(norm["planted"]["b"])
        self.assertEqual(validate_flaw_plan(_new_plan()), [])

    def test_old_shape_maps_to_new(self):
        norm = normalize_flaw_plan(_old_plan())
        # flawed_side b -> faithful a; clean_prompt -> worker + faithful render.
        self.assertEqual(norm["flawed_side"], "b")
        self.assertEqual(norm["faithful_side"], "a")
        self.assertEqual(norm["worker_prompt"], _old_plan()["clean_prompt"])
        self.assertEqual(norm["render_prompts"]["a"], _old_plan()["clean_prompt"])
        self.assertEqual(norm["render_prompts"]["b"], _old_plan()["flawed_prompt"])
        self.assertEqual(norm["planted"]["b"], _old_plan()["injected_flaws"])
        self.assertEqual(validate_flaw_plan(_old_plan()), [])

    def test_relaxed_invariant_allows_flawed_side_to_win_other_dim(self):
        # RELAXED (Phase 2): a NEW-shape plan where a dimension names the side
        # that carries a flaw on a DIFFERENT dimension is now ACCEPTED — the
        # semantic flaw->dimension mapping is deferred to the verification loop,
        # so this is no longer a structural violation. (Was rejected in Phase 1.)
        ok = _new_plan()
        ok["construction_keys"]["IF"] = "Response B"
        self.assertEqual(validate_flaw_plan(ok), [])

    def test_strict_invariant_requires_planted_on_flawed_side(self):
        bad = _new_plan()
        bad["planted"]["b"] = []
        errs = validate_flaw_plan(bad)
        self.assertTrue(any("planted" in e for e in errs), errs)

    def test_strict_invariant_requires_both_render_prompts(self):
        bad = _new_plan()
        bad["render_prompts"]["b"] = ""
        errs = validate_flaw_plan(bad)
        self.assertTrue(any("render_prompts.b" in e for e in errs), errs)
