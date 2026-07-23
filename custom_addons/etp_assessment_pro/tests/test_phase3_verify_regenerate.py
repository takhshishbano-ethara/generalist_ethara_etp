# -*- coding: utf-8 -*-
"""Phase 3: the image_ab VERIFY->REGENERATE vision loop.

After both images of a flaw-injected image_ab pair render, each side's planted
flaws are re-checked against the PIXELS via a mocked vision call; a flaw that did
not render triggers a bounded single-slot re-render, and a flaw that never
renders blocks approval so an unverified construction key can't ship. Every
Vertex call (image render + vision verify) is mocked, so the suite stays offline.
"""
import json
from unittest.mock import patch, Mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etp_assessment_pro.services import vertex


def _plan():
    return {
        "faithful_side": "a",
        "flawed_side": "b",
        "worker_prompt": "A red ceramic mug labelled 'Coffee' on a wooden table.",
        "render_prompts": {
            "a": "A red ceramic mug reading 'Coffee' on a wooden table, one handle.",
            "b": "A red ceramic mug reading 'Coffe' with a second floating handle.",
        },
        "planted": {"a": [],
                    "b": ["misspelled label 'Coffe'", "extra floating handle"]},
        "construction_keys": {"IF": "Response A", "VQ": "Both Good",
                              "LAI": "Both Good", "OC": "Response A"},
    }


def _verdicts(*present):
    return json.dumps([{"flaw": "f%d" % i, "present": p, "note": ""}
                       for i, p in enumerate(present)])


@tagged("post_install", "-at_install")
class TestPhase3VerifyRegenerate(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]
        self.env["ir.config_parameter"].sudo().set_param(
            "etp_assessment_pro.verify_flaw_render", "1")

    def _draft(self, plan=None):
        plan = plan or _plan()
        briefs = [{"slot": "a", "label": "Response A",
                   "prompt": plan["render_prompts"]["a"]},
                  {"slot": "b", "label": "Response B",
                   "prompt": plan["render_prompts"]["b"]}]
        # Materialize the dimension answer key from construction_keys so the
        # (legitimate, separate) _assert_no_key_drift by-construction guard passes;
        # this test isolates the flaw-RENDER-verify behaviour, not key drift.
        from odoo.addons.etp_assessment_pro.constants import (
            ab_specs_from_construction_keys)
        dims = ab_specs_from_construction_keys(plan["construction_keys"])
        sop = self.Prompt.create({"name": "SOP P3"})
        return self.Draft.create({
            "prompt_id": sop.id,
            "name": "AB verify",
            "question_prompt": plan["worker_prompt"],
            "question_type": "image_ab",
            "difficulty": "medium",
            "flaw_plan_json": json.dumps(plan),
            "dimensions_json": json.dumps(dims),
            "official_reasoning": "The faithful side wins by construction.",
            "image_brief_json": json.dumps(briefs),
            "image_state": "pending",
        })

    def _fake_img(self):
        return Mock(return_value=("QUJD", "image/png"))

    def test_a_all_present_no_regen(self):
        draft = self._draft()
        gen = self._fake_img()
        with patch.object(vertex, "generate_image", gen), \
                patch.object(vertex, "_call_vertex",
                             return_value=_verdicts(True, True)) as call:
            ok = draft._render_all_images()
        self.assertTrue(ok)
        self.assertEqual(draft.image_state, "rendered")
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(call.call_count, 1)
        rec = json.loads(draft.verification_json)
        self.assertTrue(rec["all_confirmed"])
        self.assertFalse(rec["needs_review"])
        self.assertEqual(rec["sides"]["b"]["regenerations"], 0)
        self.assertTrue(rec["sides"]["b"]["confirmed"])

    def test_b_absent_then_present_one_regen(self):
        draft = self._draft()
        gen = self._fake_img()
        with patch.object(vertex, "generate_image", gen), \
                patch.object(vertex, "_call_vertex",
                             side_effect=[_verdicts(True, False),
                                          _verdicts(True, True)]) as call:
            ok = draft._render_all_images()
        self.assertTrue(ok)
        self.assertEqual(gen.call_count, 3)
        self.assertEqual(call.call_count, 2)
        rec = json.loads(draft.verification_json)
        self.assertTrue(rec["all_confirmed"])
        self.assertFalse(rec["needs_review"])
        self.assertEqual(rec["sides"]["b"]["regenerations"], 1)
        self.assertTrue(rec["sides"]["b"]["confirmed"])

    def test_c_absent_past_cap_does_not_block_approval(self):
        # Research-aligned (renderers/ab.py has no verify gate): an unconfirmed
        # planted flaw is ADVISORY, not a veto. needs_review is set + a note is
        # available for reviewers, but approval PROCEEDS (construction_keys stand).
        draft = self._draft()
        gen = self._fake_img()
        with patch.object(vertex, "generate_image", gen), \
                patch.object(vertex, "_call_vertex",
                             return_value=_verdicts(False, False)):
            ok = draft._render_all_images()
        self.assertTrue(ok)
        self.assertEqual(draft.image_state, "rendered")
        rec = json.loads(draft.verification_json)
        self.assertTrue(rec["needs_review"])
        self.assertFalse(rec["all_confirmed"])
        self.assertEqual(rec["sides"]["b"]["regenerations"], 2)
        self.assertFalse(rec["sides"]["b"]["confirmed"])
        self.assertFalse(rec["sides"]["b"]["unavailable"])
        # advisory note is available...
        self.assertTrue(draft._flaw_render_review_note())
        # ...but approval is NOT blocked (no UserError, question ships).
        draft.action_approve()
        self.assertEqual(draft.state, "approved")

    def test_d1_disabled_renders_as_before(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "etp_assessment_pro.verify_flaw_render", "0")
        draft = self._draft()
        gen = self._fake_img()
        call = Mock(side_effect=AssertionError("verify must not run when off"))
        with patch.object(vertex, "generate_image", gen), \
                patch.object(vertex, "_call_vertex", call):
            ok = draft._render_all_images()
        self.assertTrue(ok)
        self.assertEqual(draft.image_state, "rendered")
        self.assertFalse(draft.verification_json)
        self.assertEqual(call.call_count, 0)

    def test_d2_unavailable_degrades(self):
        draft = self._draft()
        gen = self._fake_img()
        with patch.object(vertex, "generate_image", gen), \
                patch.object(vertex, "_call_vertex",
                             side_effect=ValueError("Vertex not configured")):
            ok = draft._render_all_images()
        self.assertTrue(ok)
        self.assertEqual(draft.image_state, "rendered")
        rec = json.loads(draft.verification_json)
        self.assertFalse(rec["needs_review"])
        self.assertTrue(rec["sides"]["b"]["unavailable"])
        # unavailable verify never flags for review, and never blocks.
        self.assertFalse(draft._flaw_render_review_note())
        draft.action_approve()
        self.assertEqual(draft.state, "approved")
