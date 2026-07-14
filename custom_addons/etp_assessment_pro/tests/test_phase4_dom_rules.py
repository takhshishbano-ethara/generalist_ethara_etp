import io
import json

from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import dom_capture, imaging, scoring


def _png_bytes(w=400, h=300, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


_BOXES = [
    {"tag": "a", "type": "", "role": "", "aria": "Home", "name": "Home",
     "text": "Home", "href": "https://x/home", "box_css": [0, 0, 80, 20]},
    {"tag": "input", "type": "search", "role": "", "aria": "Search",
     "name": "Search", "text": "", "href": "", "box_css": [100, 0, 300, 24]},
    {"tag": "button", "type": "", "role": "", "aria": "Go", "name": "Go",
     "text": "Go", "href": "", "box_css": [0, 40, 60, 64]},
]


class TestApplyOmit(TransactionCase):
    def test_omit_drops_matching_box_and_records_coverage_no(self):
        kept, omitted = dom_capture.apply_omit(
            _BOXES, {"match_tag": "input", "match_type": "search"})
        self.assertEqual(len(kept), 2)
        self.assertTrue(all(b["tag"] != "input" for b in kept))
        self.assertIsNotNone(omitted)
        self.assertEqual(omitted["tag"], "input")
        self.assertEqual(omitted["type"], "search")
        self.assertEqual(omitted["box_css"], [100, 0, 300, 24])
        self.assertIn("unboxed", omitted["reason"])

    def test_omit_matches_on_text_substring(self):
        kept, omitted = dom_capture.apply_omit(
            _BOXES, {"match_tag": "a", "match_text": "hom"})
        self.assertEqual(len(kept), 2)
        self.assertEqual(omitted["name"], "Home")

    def test_omit_no_match_is_noop(self):
        kept, omitted = dom_capture.apply_omit(
            _BOXES, {"match_tag": "select"})
        self.assertEqual(len(kept), 3)
        self.assertIsNone(omitted)

    def test_omit_empty_spec_is_noop(self):
        kept, omitted = dom_capture.apply_omit(_BOXES, None)
        self.assertEqual(len(kept), 3)
        self.assertIsNone(omitted)
        kept2, omitted2 = dom_capture.apply_omit(_BOXES, {})
        self.assertEqual(len(kept2), 3)
        self.assertIsNone(omitted2)


class TestCollectJsRules(TransactionCase):
    def test_collect_js_contains_reference_rules(self):
        js = dom_capture._COLLECT_JS
        self.assertIn("label[for]", js)
        self.assertIn("getClientRects", js)
        self.assertIn("elementFromPoint", js)
        self.assertIn("video", js)
        self.assertIn("0.9", js)
        self.assertIn("0.85", js)
        self.assertIn("/ 24", js)
        self.assertIn("aria-label", js)

    def test_capture_default_viewport_is_1440x900(self):
        import inspect
        sig = inspect.signature(dom_capture.capture_and_annotate)
        self.assertEqual(sig.parameters["viewport"].default, (1440, 900))
        self.assertEqual(sig.parameters["dsf"].default, 2)


class TestBadgePlacement(TransactionCase):
    def test_badge_stays_in_bounds_and_avoids_placed(self):
        box = [100, 100, 200, 140]
        rect, overlap = imaging.place_badge(
            box, 20, 16, [box], [], 400, 300, 0)
        self.assertGreaterEqual(rect[0], 0)
        self.assertGreaterEqual(rect[1], 0)
        self.assertLessEqual(rect[2], 400)
        self.assertLessEqual(rect[3], 300)
        self.assertEqual(overlap, 0)

    def test_second_badge_does_not_collide_with_first(self):
        boxes = [[100, 100, 200, 140], [100, 100, 200, 140]]
        r1, _ = imaging.place_badge(boxes[0], 20, 16, boxes, [], 400, 300, 0)
        r2, _ = imaging.place_badge(boxes[1], 20, 16, boxes, [r1], 400, 300, 1)
        disjoint = not imaging._rects_intersect(r1, r2)
        self.assertTrue(disjoint)

    def test_annotate_places_two_badges_and_keeps_boxpx(self):
        dets = [
            {"box_2d": [0, 0, 100, 200], "label": "one", "description": "d1"},
            {"box_2d": [0, 500, 100, 700], "label": "two", "description": "d2"}]
        png, label_key = imaging.annotate_image(_png_bytes(), dets)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual([e["number"] for e in label_key], [1, 2])
        self.assertEqual(len(label_key[0]["box_px"]), 4)


class TestCoverageGateScoring(TransactionCase):
    def _label_q_with_image(self, **img_vals):
        q = self.env["etp.assessment.pro.question"].create({
            "name": "Label", "prompt": "Label each box.",
            "question_type": "image_label"})
        vals = {"question_id": q.id, "label": "Image", "slot": "single",
                "behavioural_key_json": json.dumps([
                    {"number": 1, "element": "Home",
                     "functionality": "Opens the home page"}])}
        vals.update(img_vals)
        self.env["etp.assessment.pro.question.image"].create(vals)
        return q

    def test_coverage_gate_no_adds_constraint_and_flag(self):
        q = self._label_q_with_image(
            coverage_expected="no",
            omitted_element_json=json.dumps({
                "tag": "input", "type": "search", "text": "",
                "name": "Search", "box_css": [100, 0, 300, 24]}))
        item = scoring._build_item(
            self._resp(q, json.dumps({"1": "Opens home"})))
        self.assertEqual(item["coverage_expected"], "No")
        self.assertTrue(any("Coverage gate" in c and "'No'" in c
                            for c in item["rubric"]["constraints"]))
        self.assertTrue(any("Search" in c
                            for c in item["rubric"]["constraints"]))

    def test_no_coverage_gate_is_noop(self):
        q = self._label_q_with_image()
        item = scoring._build_item(
            self._resp(q, json.dumps({"1": "Opens home"})))
        self.assertNotIn("coverage_expected", item)

    def test_store_capture_persists_coverage_fields(self):
        q = self._label_q_with_image()
        img = q.image_ids[:1]
        img.source_url = "https://example.com"
        img.omit_spec_json = json.dumps(
            {"match_tag": "input", "match_type": "search"})
        fake = {
            "screenshot_png": _png_bytes(),
            "annotated_png": _png_bytes(color=(255, 0, 0)),
            "dom_manifest": [], "behavioural_key": [], "label_key": [],
            "omitted_element": {"tag": "input", "type": "search",
                                "box_css": [100, 0, 300, 24],
                                "reason": "unboxed"},
            "coverage_expected": "no",
        }
        captured = {}

        def _fake_capture(url, omit=None):
            captured["omit"] = omit
            return fake

        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             side_effect=_fake_capture):
            img.action_capture_url()
        img.invalidate_recordset()
        self.assertEqual(captured["omit"],
                         {"match_tag": "input", "match_type": "search"})
        self.assertEqual(img.coverage_expected, "no")
        self.assertEqual(json.loads(img.omitted_element_json)["type"], "search")
        self.assertEqual(
            scoring._image_label_coverage_expected(q), "no")

    def _resp(self, q, answer):
        applicant = self.env["hr.applicant"].create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        generator = self.env["etp.assessment.pro.prompt"].create(
            {"name": "Cat"})
        assessment = self.env["etp.assessment.pro"].create({
            "name": "A", "generator_id": generator.id})
        ev = self.env["etp.assessment.pro.evaluator"].create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})
        return self.env["etp.assessment.pro.response"].create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": answer})
