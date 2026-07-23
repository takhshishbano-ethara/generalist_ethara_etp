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


@tagged("-at_install", "post_install")
class TestDefectRenderer(TransactionCase):
    """P4: defect injection is true by construction, and every numbered marker
    lands on the ACTUAL drawn region (the fix for research's misplaced q7r
    markers, which trusted a blind marker_xy)."""

    _DEFECTS = [
        {"marker": 1, "op": "garbled_card", "marker_xy": [100, 100],
         "flaw": "garbled menu text",
         "spec": {"x": 80, "y": 90, "w": 300, "h": 380, "lines": 5, "title": "MENU"}},
        {"marker": 2, "op": "warp", "marker_xy": [900, 300],
         "flaw": "warped shelf edge",
         "spec": {"box": [850, 250, 1050, 450], "amp": 14}},
        {"marker": 3, "op": "float_copy", "marker_xy": [600, 600],
         "flaw": "cloned object",
         "spec": {"src_box": [80, 90, 380, 470], "dst_x": 500, "dst_y": 500}},
    ]

    def test_plant_returns_original_annotated_and_key(self):
        original, annotated, planted = defect_render.plant(
            _solid_png(), self._DEFECTS, seed=7)
        # both images decode as valid PNGs
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(original)).size, (1280, 720))
        self.assertEqual(Image.open(io.BytesIO(annotated)).size, (1280, 720))
        # one key entry per planted defect, renumbered 1..N in order
        self.assertEqual([p["marker"] for p in planted], [1, 2, 3])
        for p in planted:
            self.assertTrue(p.get("flaw"))

    def test_marker_center_comes_from_drawn_region_not_guess(self):
        # float_copy pastes a 300x380 region at dst (500,500); its TRUE center is
        # (650, 690), NOT the model's guessed marker_xy of (600, 600). The renderer
        # must correct the marker to the real drawn-region center.
        _o, _a, planted = defect_render.plant(_solid_png(), self._DEFECTS, seed=7)
        clone = next(p for p in planted if p["op"] == "float_copy")
        self.assertEqual(clone["marker_xy"], [650, 690])
        # and it is NOT the blind guess
        self.assertNotEqual(clone["marker_xy"], [600, 600])

    def test_bad_op_is_dropped_never_marks_empty_space(self):
        # A defect whose op raises (impossible box) is dropped from the key and
        # gets NO marker - a marker never sits on empty space.
        defects = [
            {"marker": 1, "op": "garbled_card", "marker_xy": [200, 200],
             "flaw": "ok defect",
             "spec": {"x": 80, "y": 90, "w": 200, "h": 200, "lines": 3}},
            {"marker": 2, "op": "float_copy", "marker_xy": [900, 900],
             "flaw": "broken clone",
             "spec": {"src_box": [9000, 9000, 9100, 9100], "dst_x": 8000, "dst_y": 8000}},
        ]
        _o, _a, planted = defect_render.plant(_solid_png(), defects, seed=7)
        # only the valid defect survives, renumbered to 1
        self.assertEqual(len(planted), 1)
        self.assertEqual(planted[0]["marker"], 1)
        self.assertEqual(planted[0]["flaw"], "ok defect")

    def test_unknown_op_skipped(self):
        _o, _a, planted = defect_render.plant(
            _solid_png(), [{"marker": 1, "op": "nonsense", "spec": {}}], seed=7)
        self.assertEqual(planted, [])


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
