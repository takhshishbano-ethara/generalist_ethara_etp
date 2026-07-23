# -*- coding: utf-8 -*-
"""Tests for the research-drop replication (P2 multi-pass quality + P4 true-by-
construction defect renderer). All offline / mocked -> $0 live spend.
"""
import base64
import io
import json

from odoo.tests import TransactionCase, tagged

from odoo.addons.etp_assessment_pro.services import defect_render, vertex
from odoo.addons.etp_assessment_pro.tests import vertex_fixtures as vf


def _solid_png(w=1280, h=720, colour=(180, 150, 110)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def _colour_png(w=1280, h=720):
    """A base with distinct regions so a PIL clone op produces a measurable
    pixel change (a flat solid image would clone identical pixels -> no change)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (200, 200, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([80, 90, 380, 470], fill=(40, 90, 160))
    d.ellipse([120, 140, 320, 400], fill=(230, 180, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@tagged("-at_install", "post_install")
class TestDefectRenderer(TransactionCase):
    """Drop-2 defect flow: BASE + combined EDIT + vision-ground each anchor +
    VERIFY gate (drop unverified) + renumber survivors 1..N + marker_map. All
    mocked (a fake ctx.edit_image / ctx.locate) -> $0 live spend."""

    _EDIT_DEFECTS = [
        {"marker": 1, "kind": "text", "method": "edit",
         "edit_instruction": "make the menu read garbled letters",
         "anchor": "the menu card on the table", "flaw": "garbled menu text"},
        {"marker": 2, "kind": "anatomy", "method": "edit",
         "edit_instruction": "give the hand a sixth finger",
         "anchor": "the right hand holding the cup", "flaw": "hand has six fingers"},
        {"marker": 3, "kind": "continuity", "method": "edit",
         "edit_instruction": "remove the person from the mirror reflection",
         "anchor": "the wall mirror", "flaw": "reflection omits the subject"},
    ]

    def _ctx(self, present_anchors):
        """A fake render ctx: edit_image echoes the base; locate returns a box for
        every anchor but only marks `defect_present` for anchors in the set."""
        outer = self

        class _Ctx:
            edits = 0
            locates = 0

            def edit_image(self, prompt, ref_png_bytes):
                type(self).edits += 1
                return ref_png_bytes  # echo — the "edit" leaves a valid PNG

            def locate(self, image_png_bytes, anchor, flaw=""):
                type(self).locates += 1
                # deterministic box near the middle, unique per anchor length
                y = 300 + (len(anchor) % 5) * 40
                return {"found": True, "box_2d": [y, 400, y + 80, 520],
                        "defect_present": anchor in present_anchors}
        return _Ctx()

    def test_verify_gate_drops_unverified_and_renumbers(self):
        # only 2 of the 3 anchors verify present -> the 3rd is DROPPED and the
        # survivors renumbered 1..2 (continuous, no gaps).
        present = {"the menu card on the table", "the wall mirror"}
        ctx = self._ctx(present)
        original, annotated, info = defect_render.plant(
            _solid_png(), self._EDIT_DEFECTS, ctx=ctx, seed=7)
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(original)).size, (1280, 720))
        self.assertEqual(Image.open(io.BytesIO(annotated)).size, (1280, 720))
        # exactly the 2 verified defects survive, renumbered 1..2
        self.assertEqual(info["planted_markers"], [1, 2])
        self.assertEqual(len(info["defects"]), 2)
        # one combined edit call, one locate per defect
        self.assertEqual(ctx.edits, 1)
        self.assertEqual(ctx.locates, 3)
        # marker_map remaps the ORIGINAL markers (1 and 3) to the new 1..2
        self.assertEqual(set(info["marker_map"].values()), {1, 2})

    def test_all_present_keeps_all(self):
        present = {d["anchor"] for d in self._EDIT_DEFECTS}
        ctx = self._ctx(present)
        _o, _a, info = defect_render.plant(
            _solid_png(), self._EDIT_DEFECTS, ctx=ctx, seed=7)
        self.assertEqual(info["planted_markers"], [1, 2, 3])
        self.assertTrue(info["assets_verified"])

    def test_marker_lands_on_located_box_center(self):
        # the marker for a verified defect is placed at the box_2d center in px,
        # NOT a pre-guessed coordinate. box [340,400,420,520] on the 0-1000 grid
        # -> center ((400+520)/2/1000*1280, (340+420)/2/1000*720) = (588, 273).
        present = {"the menu card on the table"}

        class _Ctx:
            def edit_image(self, prompt, ref):
                return ref

            def locate(self, img, anchor, flaw=""):
                return {"found": True, "box_2d": [340, 400, 420, 520],
                        "defect_present": True}
        _o, _a, info = defect_render.plant(
            _solid_png(), [self._EDIT_DEFECTS[0]], ctx=_Ctx(), seed=7)
        self.assertEqual(info["defects"][0]["marker_xy"], [588, 273])

    def test_no_ctx_degrades_without_crash(self):
        # with no ctx (no creds / offline) edit defects cannot be verified, so
        # nothing is drawn — but the call must not crash and must still return a
        # valid original image.
        _o, _a, info = defect_render.plant(
            _solid_png(), self._EDIT_DEFECTS, ctx=None, seed=7)
        self.assertEqual(info["planted_markers"], [])

    def test_pil_clone_fallback_lane(self):
        # a genuine clone/duplication defect uses the PIL lane (method=pil,
        # float_copy) and is verified by pixel-change, needing no edit/locate.
        defects = [{"marker": 1, "kind": "continuity", "method": "pil",
                    "op": "float_copy", "marker_xy": [650, 690],
                    "flaw": "cloned cup",
                    "spec": {"src_box": [80, 90, 380, 470],
                             "dst_x": 500, "dst_y": 500}}]
        _o, _a, info = defect_render.plant(
            _colour_png(), defects, ctx=None, seed=7)
        self.assertEqual(len(info["defects"]), 1)
        self.assertEqual(info["defects"][0]["marker"], 1)


@tagged("-at_install", "post_install")
class TestMetadataTruncationRobust(TransactionCase):
    """Grounding (evidence + required_elements) must survive a MAX_TOKENS-truncated
    envelope: the metadata block is emitted first and is usually complete, so we
    recover it even when questions/solutions are cut off."""

    def test_recovers_metadata_from_truncated_envelope(self):
        truncated = (
            '{"metadata": {"evidence": [{"id": "E1", "quote": "drop a dot on each '
            'defect"}], "required_elements": [{"id": "coverage", "statement": '
            '"marks every tell"}], "mapping": ["domain:image-annotation"]}, '
            '"questions": [{"name": "Q1", "prompt": "this got cut off mid')
        meta = vertex._extract_metadata_object(truncated)
        self.assertIsInstance(meta, dict)
        self.assertEqual(len(meta["evidence"]), 1)
        self.assertEqual(len(meta["required_elements"]), 1)
        self.assertEqual(meta["mapping"], ["domain:image-annotation"])

    def test_capture_persists_grounding_from_truncated(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "Trunc SOP"})
        truncated = (
            '{"metadata": {"sop_title": "T", "evidence": [{"id": "E1", "quote": '
            '"q"}], "required_elements": [{"id": "e1", "statement": "s", '
            '"evidence": "E1"}]}, "questions": [{"name": "cut')
        vertex._capture_sop_metadata(self.env, prompt, truncated)
        prompt.invalidate_recordset()
        self.assertTrue(prompt.evidence_json)
        self.assertTrue(prompt.required_elements_json)
        self.assertEqual(len(json.loads(prompt.evidence_json)), 1)


@tagged("-at_install", "post_install")
class TestDefectDraftFields(TransactionCase):
    """P4 wiring: a base_prompt + defects image_spec produces a defect_plan_json
    draft, not a source_url/dense draft."""

    def test_build_defect_draft_fields(self):
        item = {
            "name": "Mark the AI defects",
            "question_type": "image_label",
            "image_specs": {
                "base_prompt": "a cafe table with a menu card and a coffee cup",
                "defects": [
                    {"marker": 1, "kind": "text", "op": "garbled_card",
                     "marker_xy": [230, 280], "flaw": "garbled menu text",
                     "spec": {"x": 80, "y": 90, "w": 300, "h": 380, "lines": 5}},
                    {"marker": 2, "kind": "duplication", "op": "float_copy",
                     "marker_xy": [650, 690], "flaw": "cloned cup",
                     "spec": {"src_box": [80, 90, 380, 470], "dst_x": 500, "dst_y": 500}},
                ],
                "answer_key": {"ideal_labels": {"1": "garbled text", "2": "clone"},
                               "decoys": ["the real cup"]},
            },
        }
        vals = vertex._build_image_draft_fields(self.env, "image_label", item)
        self.assertTrue(vals.get("defect_plan_json"))
        plan = json.loads(vals["defect_plan_json"])
        self.assertEqual(plan["base_prompt"],
                         "a cafe table with a menu card and a coffee cup")
        self.assertEqual(len(plan["defects"]), 2)
        # base_prompt becomes the single image brief; NOT a source_url draft
        self.assertFalse(vals.get("source_url"))
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual(briefs[0]["slot"], "single")


@tagged("-at_install", "post_install")
class TestProjectAwareScoring(TransactionCase):
    """Scoring rework (drop 2): deterministic project detection + start-at-100
    deduction accounting cross-check. Pure functions, offline -> $0 live spend."""

    def test_detect_project_by_title_signals(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        self.assertEqual(
            scoring.detect_project("Q7r Guidelines", "place a dot on each defect",
                                   "image_label", defect_context=True),
            "P2 AI DEFECT ANNOTATION")
        self.assertEqual(
            scoring.detect_project("Text to Image Compare - SOP", "", "image_ab"),
            "P1 IMAGE A/B COMPARE")
        self.assertEqual(
            scoring.detect_project("Video Artistic Style", "", "video_prompt"),
            "P5 PROMPT WRITING — VIDEO")

    def test_detect_project_type_fallback_when_no_title_signal(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        # a novel SOP whose title lacks a known signal still routes by task type
        # (our self-extending divergence), not straight to GENERAL.
        self.assertEqual(
            scoring.detect_project("Some New Task", "", "mcq"),
            "P6 MULTIPLE CHOICE")
        self.assertEqual(
            scoring.detect_project("Some New Task", "", "image_label",
                                   defect_context=False),
            "P3 DENSE UI LABELLING")
        self.assertEqual(
            scoring.detect_project("Totally Unknown", "", "subjective_rubric"),
            "GENERAL")

    def test_recompute_deductions_sums_to_score(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        it = {"score": 68, "deductions": [
            {"points": -16, "reason": "vague", "evidence": "looks weird"},
            {"points": -16, "reason": "false positive", "evidence": "six fingers"},
        ]}
        score, note = scoring._recompute_deductions(it)
        self.assertEqual(score, 68.0)          # 100 + (-16) + (-16)
        self.assertIsNone(note)                # judge score matches the recompute

    def test_recompute_deductions_flags_judge_drift(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        # judge claims 90 but the itemized deductions only sum to 100-16 = 84
        it = {"score": 90, "deductions": [{"points": -16, "reason": "x"}]}
        score, note = scoring._recompute_deductions(it)
        self.assertEqual(score, 84.0)
        self.assertTrue(note and "recompute" in note)

    def test_empty_deductions_is_perfect_only_when_score_100(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        # empty deductions + emitted 100 => trusted perfect
        s, _n = scoring._recompute_deductions({"score": 100, "deductions": []})
        self.assertEqual(s, 100.0)
        # empty deductions + a NON-100 emitted score => cannot verify, defer
        s2, _n2 = scoring._recompute_deductions({"score": 55, "deductions": []})
        self.assertIsNone(s2)

    def test_no_deductions_array_defers_to_v10(self):
        from odoo.addons.etp_assessment_pro.services import scoring
        # a legacy result with no deductions array returns (None, None) so the
        # caller falls back to the v10 recompute.
        s, n = scoring._recompute_deductions({"score": 70})
        self.assertIsNone(s)
        self.assertIsNone(n)
