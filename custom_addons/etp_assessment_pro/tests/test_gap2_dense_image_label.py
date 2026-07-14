import base64
import io
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex, scoring


def _png_bytes(w=200, h=150, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _dense_specs(coverage="yes", omitted=None, application="Acme Bank dashboard"):
    boxes = [
        {"number": 1, "box_2d": [40, 30, 90, 300], "element": "Search field",
         "functionality": "Focuses the search field to type a query"},
        {"number": 2, "box_2d": [40, 820, 90, 980], "element": "Cart icon",
         "functionality": "Opens the shopping cart"},
        {"number": 3, "box_2d": [200, 30, 260, 480], "element": "Transfer button",
         "functionality": "Starts a money transfer"},
    ]
    specs = {
        "images": [{"slot": "single", "label": "Screenshot",
                    "prompt": "A banking dashboard with a search field top-left, "
                              "a cart icon top-right, and a Transfer button."}],
        "application": application,
        "coverage_expected": coverage,
        "boxes": boxes,
        "answer_key": {
            "ideal_labels": {"1": "Focuses the search field to type a query",
                             "2": "Opens the shopping cart",
                             "3": "Starts a money transfer"},
            "mandatory_elements": ["search", "cart", "transfer"],
            "penalty_rules": ["no hallucinated controls"],
            "scoring_guide": "Reward correct per-box actions."},
    }
    if omitted:
        specs["omitted_element"] = omitted
    return specs


def _dense_item(coverage="yes", omitted=None, application="Acme Bank dashboard"):
    return {
        "name": "Label the dashboard", "question_type": "image_label",
        "prompt": "Number every interactive control, describe what each does, "
                  "and name the application.",
        "difficulty": "medium",
        "image_specs": _dense_specs(coverage, omitted, application)}


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.Image = self.env["etp.assessment.pro.question.image"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        self.Draft = self.env["etp.assessment.pro.prompt.question"]

    def _label_q(self, behavioural_key=None, coverage=None, omitted=None,
                 application=None, answer_key=None):
        q = self.Question.create({
            "name": "Label boxes", "prompt": "Identify each numbered box.",
            "question_type": "image_label",
            "subjective_rubric_json":
                json.dumps(answer_key) if answer_key else False})
        self.Image.create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "behavioural_key_json":
                json.dumps(behavioural_key) if behavioural_key else False,
            "coverage_expected": coverage or False,
            "omitted_element_json":
                json.dumps(omitted) if omitted else False,
            "label_application": application or False})
        return q

    def _resp(self, q, answer):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        generator = self.Prompt.create({"name": "Img Cat"})
        assessment = self.Assessment.create({
            "name": "Img Assessment", "generator_id": generator.id})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})
        return self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": answer})

    def _v6_payload(self, resp, score):
        return json.dumps({
            "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
            "submission_flags": [], "results": [{
                "item_id": str(resp.id), "id": resp.id,
                "field_key": "justification", "skills": [],
                "rubric_source": "supplied", "rubric": {}, "gate": "none",
                "reference_answer": "ref", "reasoning": "audit",
                "verdict_consistency": "match", "flags": [],
                "score": score, "passed": score >= 0.70,
                "feedback": "graded"}]})


class TestDenseGeneration(_Base):
    def test_dense_build_emits_behavioural_key_boxes_coverage_application(self):
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", _dense_item())
        bkey = json.loads(vals["behavioural_key_json"])
        self.assertEqual([e["number"] for e in bkey], [1, 2, 3])
        self.assertEqual(bkey[1]["functionality"], "Opens the shopping cart")
        self.assertNotIn("box_2d", bkey[0])
        geometry = json.loads(vals["label_boxes_json"])
        self.assertEqual(geometry[0]["box_2d"], [40, 30, 90, 300])
        self.assertEqual(vals["coverage_expected"], "yes")
        self.assertEqual(vals["label_application"], "Acme Bank dashboard")
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["slot"], "single")
        key = json.loads(vals["rubric_json"])
        self.assertIsInstance(key["ideal_labels"], str)
        self.assertIn("Opens the shopping cart", key["ideal_labels"])

    def test_dense_item_validates(self):
        self.assertEqual(
            vertex._validate_question_item(_dense_item(), "image_label"), [])

    def test_map_only_without_coords_falls_back_to_legacy(self):
        item = _dense_item()
        item["image_specs"].pop("boxes")
        self.assertEqual(
            vertex._validate_question_item(item, "image_label"), [])
        vals = vertex._build_image_draft_fields(self.env, "image_label", item)
        self.assertNotIn("behavioural_key_json", vals)
        self.assertIn("rubric_json", vals)

    def test_generate_from_sop_persists_dense_fields_on_draft(self):
        prompt = self.Prompt.create({"name": "SOP P"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": prompt.id, "name": "sop.pdf",
            "file": base64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        payload = json.dumps([_dense_item()])
        with patch.object(vertex, "_call_vertex", return_value=payload):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        self.assertEqual(len(draft_ids), 1)
        draft = self.Draft.browse(draft_ids)
        self.assertEqual(draft.question_type, "image_label")
        self.assertTrue(draft.behavioural_key_json)
        self.assertEqual(draft.coverage_expected, "yes")
        self.assertEqual(draft.label_application, "Acme Bank dashboard")
        self.assertEqual(draft.image_state, "pending")


class TestCoverageNoGate(_Base):
    _OMITTED = {"tag": "button", "text": "Log out",
                "reason": "left unboxed so coverage is No"}

    def test_dense_no_records_coverage_and_omitted(self):
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", _dense_item(coverage="no",
                                                 omitted=self._OMITTED))
        self.assertEqual(vals["coverage_expected"], "no")
        self.assertEqual(
            json.loads(vals["omitted_element_json"])["text"], "Log out")

    def test_scoring_consumes_coverage_no_gate(self):
        bkey = [{"number": 1, "element": "Search field",
                 "functionality": "Focuses the search field"},
                {"number": 2, "element": "Cart icon",
                 "functionality": "Opens the shopping cart"}]
        q = self._label_q(behavioural_key=bkey, coverage="no",
                          omitted=self._OMITTED)
        resp = self._resp(q, json.dumps({"1": "Focuses search", "2": "Opens cart"}))
        item = scoring._build_item(resp)
        self.assertEqual(item["coverage_expected"], "No")
        self.assertTrue(any("Log out" in c and "unboxed" in c
                            for c in item["rubric"]["constraints"]))


class TestDensePerBoxScoring(_Base):
    def test_per_box_rubric_uses_behavioural_key_and_application(self):
        bkey = json.loads(vertex._build_image_draft_fields(
            self.env, "image_label", _dense_item())["behavioural_key_json"])
        q = self._label_q(behavioural_key=bkey, coverage="yes",
                          application="Acme Bank dashboard")
        resp = self._resp(q, json.dumps(
            {"1": "type a query", "2": "opens cart", "3": "starts transfer"}))
        item = scoring._build_item(resp)
        checklist = item["rubric"]["checklist"]
        self.assertEqual(len(checklist), 4)                 # 3 boxes + app
        self.assertIn("Box 2", checklist[1])
        self.assertIn("Opens the shopping cart", checklist[1])
        self.assertTrue(any("Acme Bank dashboard" in c for c in checklist))
        self.assertEqual(item["coverage_expected"], "Yes")
        self.assertNotIn("ideal_labels", item)

    def test_dense_scored_with_coverage_and_correctness(self):
        bkey = [{"number": n, "element": "e%d" % n,
                 "functionality": "does %d" % n} for n in (1, 2, 3)]
        q = self._label_q(behavioural_key=bkey, coverage="yes")
        resp = self._resp(q, json.dumps({"1": "does 1", "2": "does 2",
                                         "3": "does 3"}))
        with patch.object(vertex, "_call_vertex",
                          return_value=self._v6_payload(resp, 0.90)):
            scored = scoring._score_image_label_items(self.env, resp)
        self.assertEqual(scored, 1)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 90.0)
        audit = json.loads(resp.llm_result_json)
        self.assertEqual(audit["label_scores"]["total_boxes"], 3)
        self.assertEqual(audit["label_scores"]["attempted_boxes"], 3)
        self.assertEqual(audit["label_scores"]["coverage"], 1.0)

    def test_low_coverage_still_caps_dense_at_40(self):
        bkey = [{"number": n, "element": "e%d" % n,
                 "functionality": "does %d" % n} for n in range(1, 5)]
        q = self._label_q(behavioural_key=bkey, coverage="yes")
        resp = self._resp(q, json.dumps({"1": "does 1"}))
        with patch.object(vertex, "_call_vertex",
                          return_value=self._v6_payload(resp, 0.95)):
            scoring._score_image_label_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_raw_100, 40.0)


class TestLegacySingleBox(_Base):
    _LEGACY = {"ideal_labels": "A red car and a brown dog.",
               "mandatory_elements": ["car", "dog"],
               "penalty_rules": ["no hallucinated objects"],
               "scoring_guide": "Award for accuracy."}

    def test_legacy_build_has_no_behavioural_key(self):
        item = {"name": "L", "question_type": "image_label",
                "prompt": "Label the photo.",
                "image_specs": {
                    "images": [{"slot": "single", "label": "Image",
                                "prompt": "a red car and a brown dog"}],
                    "answer_key": self._LEGACY}}
        self.assertEqual(
            vertex._validate_question_item(item, "image_label"), [])
        vals = vertex._build_image_draft_fields(self.env, "image_label", item)
        self.assertNotIn("behavioural_key_json", vals)
        self.assertNotIn("coverage_expected", vals)
        self.assertEqual(
            json.loads(vals["rubric_json"])["ideal_labels"],
            "A red car and a brown dog.")

    def test_legacy_scores_unchanged(self):
        q = self._label_q(answer_key=self._LEGACY)
        resp = self._resp(q, "A red car and a brown dog.")
        item = scoring._build_item(resp)
        self.assertIn("ideal_labels", item)                 # answer-key fallback
        self.assertNotIn("checklist", item.get("rubric", {}))
        with patch.object(vertex, "_call_vertex",
                          return_value=self._v6_payload(resp, 0.80)):
            scoring._score_image_label_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 80.0)


class TestDenseApproveDrawsBoxes(_Base):
    def test_approve_draws_boxes_and_carries_authored_key(self):
        prompt = self.Prompt.create({"name": "GenP"})
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", _dense_item())
        png_b64 = base64.b64encode(_png_bytes()).decode()
        draft = self.Draft.create({
            "prompt_id": prompt.id, "name": "Dense draft",
            "question_type": "image_label",
            "question_prompt": "Number the controls.",
            "rubric_json": vals["rubric_json"],
            "behavioural_key_json": vals["behavioural_key_json"],
            "label_boxes_json": vals["label_boxes_json"],
            "coverage_expected": vals["coverage_expected"],
            "label_application": vals["label_application"],
            "image_brief_json": vals["image_brief_json"],
            "images_json": json.dumps([
                {"slot": "single", "label": "Screenshot",
                 "data": "data:image/png;base64,%s" % png_b64}]),
            "image_state": "rendered"})
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        img = draft.approved_question_id.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        self.assertTrue(img.behavioural_key_json)
        self.assertEqual(img.coverage_expected, "yes")
        self.assertEqual(img.label_application, "Acme Bank dashboard")
        label_key = json.loads(img.detections_json)
        self.assertEqual([e["number"] for e in label_key], [1, 2, 3])
        self.assertTrue(img.annotated_image)
