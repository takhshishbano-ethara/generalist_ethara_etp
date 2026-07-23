import base64
import io
import json
import unittest
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

from odoo.addons.etp_assessment_pro.services import (
    vertex, scoring, dom_capture, imaging)


def _png_bytes(w=200, h=150, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.Image = self.env["etp.assessment.pro.question.image"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]

    def _evaluator(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        generator = self.env["etp.assessment.pro.prompt"].create(
            {"name": "Img Cat"})
        assessment = self.Assessment.create({
            "name": "Img Assessment", "generator_id": generator.id})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})
        return ev, applicant, assessment

    def _label_q(self, behavioural_key=None, detections=None, answer_key=None):
        q = self.Question.create({
            "name": "Label boxes", "prompt": "Identify each numbered box.",
            "question_type": "image_label",
            "subjective_rubric_json":
                json.dumps(answer_key) if answer_key else False})
        self.Image.create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "behavioural_key_json":
                json.dumps(behavioural_key) if behavioural_key else False,
            "detections_json":
                json.dumps(detections) if detections else False})
        return q

    def _resp(self, q, answer):
        ev, applicant, assessment = self._evaluator()
        return self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": answer})

    def _v6_payload(self, resp, score, **extra):
        result = {
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "rubric_source": "supplied", "rubric": {}, "gate": "none",
            "reference_answer": "ref", "reasoning": "audit",
            "verdict_consistency": "match", "flags": [],
            "score": score, "passed": score >= 0.70, "feedback": "graded"}
        result.update(extra)
        return json.dumps({
            "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
            "submission_flags": [], "results": [result]})


_BKEY = [
    {"number": 1, "element": "English",
     "functionality": "Opens the English Wikipedia"},
    {"number": 2, "element": "Deutsch",
     "functionality": "Opens the German Wikipedia"},
]

_DETECTIONS = [
    {"number": 1, "label": "car", "description": "a red car",
     "box_px": [20, 15, 80, 60]},
    {"number": 2, "label": "dog", "description": "a brown dog",
     "box_px": [10, 120, 60, 180]},
]


class TestCaptureButtonGuards(_Base):
    def test_capture_url_requires_source_url(self):
        q = self._label_q()
        img = q.image_ids[:1]
        self.assertFalse(img.source_url)
        with self.assertRaises(UserError):
            img.action_capture_url()

    def test_capture_url_requires_playwright(self):
        q = self._label_q()
        img = q.image_ids[:1]
        img.source_url = "https://example.com"
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False):
            with self.assertRaises(UserError):
                img.action_capture_url()

    def test_capture_url_stores_capture_when_available(self):
        q = self._label_q()
        img = q.image_ids[:1]
        img.source_url = "https://example.com"
        fake = {
            "screenshot_png": _png_bytes(),
            "annotated_png": _png_bytes(color=(255, 0, 0)),
            "dom_manifest": [{"number": 1, "tag": "a", "role": "",
                              "name": "English", "text": "English",
                              "href": "https://en.wikipedia.org", "box_css":
                              [0, 0, 80, 20], "in_shadow": False,
                              "boxed_via_label": False}],
            "behavioural_key": _BKEY,
            "label_key": _DETECTIONS,
        }
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             return_value=fake) as m:
            img.action_capture_url()
        m.assert_called_once_with("https://example.com", omit=None)
        img.invalidate_recordset()
        self.assertTrue(img.image)
        self.assertTrue(img.annotated_image)
        self.assertEqual(json.loads(img.behavioural_key_json), _BKEY)
        self.assertEqual(len(json.loads(img.dom_manifest_json)), 1)
        self.assertEqual(len(json.loads(img.detections_json)), 2)
        self.assertIn("@2x", img.capture_viewport)


class TestBehaviouralRubric(_Base):
    def test_rubric_grades_behaviour_not_nominal_name(self):
        q = self._label_q(behavioural_key=_BKEY)
        resp = self._resp(q, json.dumps(
            {"1": "Opens English Wikipedia", "2": "Opens German Wikipedia"}))
        item = scoring._build_item(resp)
        self.assertEqual(item["rubric_source_hint"], "supplied")
        checklist = item["rubric"]["checklist"]
        self.assertEqual(len(checklist), 2)
        self.assertIn("Opens the German Wikipedia", checklist[1])
        self.assertIn("Deutsch", checklist[1])
        self.assertTrue(any("ACTION" in c or "behaviour" in c
                            for c in item["rubric"]["constraints"]))
        self.assertIn("Box 1: Opens English Wikipedia", item["candidate_text"])
        self.assertNotIn("ideal_labels", item)

    def test_behavioural_key_scored_via_mocked_judge(self):
        q = self._label_q(behavioural_key=_BKEY)
        resp = self._resp(q, json.dumps(
            {"1": "Opens English Wikipedia", "2": "Opens German Wikipedia"}))
        with patch.object(vertex, "_call_vertex",
                          return_value=self._v6_payload(resp, 0.90)):
            scored = scoring._score_image_label_items(self.env, resp)
        self.assertEqual(scored, 1)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 90.0)
        audit = json.loads(resp.llm_result_json)
        self.assertEqual(audit["label_scores"]["coverage"], 1.0)
        self.assertEqual(audit["label_scores"]["attempted_boxes"], 2)
        self.assertEqual(audit["label_scores"]["total_boxes"], 2)

    def test_low_coverage_flags_review_keeps_parity_score(self):
        # PARITY (research trusts the judge): no more coverage cap-40 double-penalty
        # (research scores image_label via its own 100*correct/total deductions).
        # A lenient judge score (95 on 1/4 boxes) is KEPT as the mark, and the
        # coverage mismatch is FLAGGED needs_review for an admin.
        four = _BKEY + [
            {"number": 3, "element": "Espanol",
             "functionality": "Opens the Spanish Wikipedia"},
            {"number": 4, "element": "Search",
             "functionality": "Focuses the field to type: Search"}]
        q = self._label_q(behavioural_key=four)
        resp = self._resp(q, json.dumps({"1": "Opens English Wikipedia"}))
        with patch.object(vertex, "_call_vertex",
                          return_value=self._v6_payload(resp, 0.95)):
            scoring._score_image_label_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertEqual(resp.llm_raw_100, 95.0)          # parity: not clamped
        flags = json.loads(resp.llm_flags_json or "[]")
        self.assertIn("needs_review", flags)              # but flagged for review
        audit = json.loads(resp.llm_result_json)
        self.assertEqual(audit["label_scores"]["attempted_boxes"], 1)
        self.assertEqual(audit["label_scores"]["total_boxes"], 4)
        self.assertEqual(audit["label_scores"]["coverage_ceiling"], 25.0)

    def test_fallback_to_detections_rubric_without_behavioural_key(self):
        q = self._label_q(detections=_DETECTIONS)
        resp = self._resp(q, json.dumps({"1": "car", "2": "dog"}))
        item = scoring._build_item(resp)
        checklist = item["rubric"]["checklist"]
        self.assertEqual(len(checklist), 2)
        self.assertIn("correctly identified as", checklist[0])
        self.assertFalse(scoring._image_label_behavioural_key(q))
        self.assertTrue(any("hallucinated" in c.lower()
                            for c in item["rubric"]["constraints"]))


class TestDomCapturePureHelpers(_Base):
    def test_draft_functionality_variants(self):
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "a", "href": "https://x", "name": "German Wikipedia"}
        ).startswith("Opens German Wikipedia"))
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "input", "type": "search", "name": "Query"}
        ).startswith("Focuses the field to type"))
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "select", "name": "Lang"}
        ).startswith("Opens the option list"))
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "summary", "name": "More"}
        ).startswith("Expands or collapses"))
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "input", "type": "checkbox", "name": "Agree",
             "boxed_via_label": True}
        ).startswith("Toggles"))
        self.assertTrue(dom_capture.draft_functionality(
            {"tag": "button", "name": "Go"}).startswith("Activates"))

    def test_coordinate_adapter_maps_css_px_to_0_1000(self):
        boxes = [{"box_css": [0, 0, 800, 500], "tag": "a",
                  "href": "https://x", "name": "link"}]
        dets = dom_capture._boxes_to_detections(boxes, 3200, 2000, 2)
        self.assertEqual(len(dets), 1)
        self.assertEqual(dets[0]["box_2d"], [0, 0, 500, 500])
        self.assertEqual(dets[0]["label"], "link")
        self.assertEqual(dets[0]["description"], "Opens link")

    def test_capture_raises_without_playwright(self):
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                dom_capture.capture_and_annotate("https://example.com")

    def test_build_manifest_and_key_numbers_in_order(self):
        boxes = [
            {"tag": "a", "href": "https://en", "name": "English",
             "text": "English", "box_css": [0, 0, 80, 20]},
            {"tag": "a", "href": "https://de", "name": "Deutsch",
             "text": "Deutsch", "box_css": [0, 30, 80, 50]}]
        manifest, key = dom_capture._build_manifest_and_key(boxes)
        self.assertEqual([m["number"] for m in manifest], [1, 2])
        self.assertEqual([k["number"] for k in key], [1, 2])
        self.assertEqual(key[1]["element"], "Deutsch")
        self.assertEqual(key[1]["functionality"], "Opens Deutsch")


@unittest.skipUnless(dom_capture.PLAYWRIGHT_AVAILABLE,
                     "Playwright/Chromium not installed in this environment")
class TestCaptureSmoke(_Base):
    def test_capture_data_url_produces_boxes_and_key(self):
        html = ("data:text/html,<html><body>"
                "<a href='https://en.wikipedia.org'>English</a> "
                "<button>Go</button></body></html>")
        result = dom_capture.capture_and_annotate(html, viewport=(400, 300))
        self.assertTrue(result["screenshot_png"].startswith(b"\x89PNG"))
        self.assertTrue(result["annotated_png"].startswith(b"\x89PNG"))
        self.assertGreaterEqual(len(result["behavioural_key"]), 1)
        self.assertEqual(len(result["behavioural_key"]),
                         len(result["dom_manifest"]))
