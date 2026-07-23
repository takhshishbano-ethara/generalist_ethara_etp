import base64
import io
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import (
    vertex, scoring, consistency, imaging, dom_capture, image_ingest,
    s3_service)
from odoo.addons.etp_assessment_pro.controllers import portal as portal_ctrl


def _png_bytes(w=200, h=150, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeRequest:
    def __init__(self, env):
        self.env = env


class _Base(TransactionCase):
    IF = "Instruction Following (IF)"
    VQ = "Visual Quality (VQ)"
    LAI = "Label Accuracy (LAI)"
    OC = "Overall Choice (OC)"

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.AB_OPTS = ["Response A", "Response B", "Both Good",
                        "Both Bad", "Tie"]

    def _opt(self, qd, label):
        return qd.option_line_ids.filtered(lambda o: o.name == label)[:1]

    def _attach_dim(self, question, axis_name, correct_label, options=None):
        options = options or self.AB_OPTS
        return self.QDim.create({
            "question_id": question.id,
            "name": axis_name,
            "option_line_ids": [
                (0, 0, {"name": o, "sequence": (i + 1) * 10,
                        "is_correct": o == correct_label})
                for i, o in enumerate(options)
            ],
        })

    def _lines_for(self, question, picks):
        by_name = {qd.name: qd for qd in question.question_dimension_ids}
        return [(0, 0, {
            "question_dimension_id": by_name[name].id,
            "selected_option_id": self._opt(by_name[name], label).id})
            for name, label in picks]

    def _build_image_ab(self, official):
        q = self.Question.create({
            "name": "AB Eval",
            "prompt": "Compare Response A and Response B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction and is sharper.",
        })
        for axis_name, label in official:
            self._attach_dim(q, axis_name, label)
        self.env["etp.assessment.pro.question.image"].create([
            {"question_id": q.id, "label": "Response A", "slot": "a"},
            {"question_id": q.id, "label": "Response B", "slot": "b"},
        ])
        return q

    def _evaluator(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        category = self.env["etp.assessment.pro.prompt"].create(
            {"name": "Img Cat"})
        assessment = self.Assessment.create({
            "name": "Img Assessment", "generator_id": category.id})
        return self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
        }), applicant, assessment


class TestImageModelAndSeed(_Base):
    def test_image_model_and_seed(self):
        q = self._build_image_ab([
            (self.IF, "Response A"),
            (self.VQ, "Response B"),
            (self.LAI, "Response A"),
            (self.OC, "Response A"),
        ])
        self.assertEqual(len(q.image_ids), 2)
        self.assertEqual(set(q.image_ids.mapped("slot")), {"a", "b"})
        self.assertEqual(len(q.question_dimension_ids), 4)
        for qd in q.question_dimension_ids:
            names = qd.option_line_ids.mapped("name")
            for label in ("Response A", "Response B", "Both Good",
                          "Both Bad", "Tie"):
                self.assertIn(label, names)


class TestImageAbObjectiveScore(_Base):
    def test_image_ab_has_no_code_objective_pool(self):
        # EQUAL MARKS: image_ab is graded as a single LLM mark, NOT a
        # code-objective partial-credit pool. So _compute_score yields 0/0 and
        # the response needs_llm (the axis picks are LLM grading input).
        q = self._build_image_ab([
            (self.IF, "Response A"),
            (self.VQ, "Response B"),
            (self.LAI, "Response A"),
            (self.OC, "Response A"),
        ])
        ev, applicant, assessment = self._evaluator()
        picks = [
            (self.IF, "Response A"),
            (self.VQ, "Response A"),
            (self.LAI, "Response A"),
            (self.OC, "Response A"),
        ]
        line_vals = self._lines_for(q, picks)
        resp = self.Response.create({
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": q.id,
            "justification": "A is sharper and more realistic than B.",
            "line_ids": line_vals,
        })
        self.assertEqual(resp.max_score, 0)
        self.assertEqual(resp.score, 0)
        self.assertFalse(resp.has_objective)
        self.assertTrue(resp.needs_llm)

    def test_image_ab_graded_on_verdicts_without_justification(self):
        # The fix: an image_ab answered with verdicts but NO justification must
        # STILL be graded (the verdicts are the answer) and surface them.
        q = self._build_image_ab([
            (self.IF, "Response A"), (self.VQ, "Response B"),
            (self.LAI, "Response A"), (self.OC, "Response A")])
        q2 = self._build_image_ab([
            (self.IF, "Response A"), (self.VQ, "Response B"),
            (self.LAI, "Response A"), (self.OC, "Response A")])
        ev, applicant, assessment = self._evaluator()
        picks = [(self.IF, "Response A"), (self.VQ, "Response A"),
                 (self.LAI, "Response A"), (self.OC, "Response A")]
        lines = self._lines_for(q, picks)
        r = self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": "", "line_ids": lines})
        self.assertTrue(r.needs_llm)              # graded on verdicts alone
        self.assertIn("Response A", r.answer_summary)  # verdicts surface to UI
        r2 = self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q2.id,
            "justification": "", "line_ids": []})
        self.assertFalse(r2.needs_llm)            # no verdicts -> nothing to grade


class TestConsistencyService(_Base):
    def test_oc_mismatch(self):
        result = consistency.consistency_checker(
            {"OC": "A"}, "Response B is better overall; B wins.")
        codes = [f["code"] for f in result["flags"]]
        self.assertIn("oc_mismatch", codes)
        self.assertEqual(result["severity"], "critical")

    def test_vq_missing_support(self):
        result = consistency.consistency_checker(
            {"VQ": "A"}, "I just preferred it, no specific reason given.")
        codes = [f["code"] for f in result["flags"]]
        self.assertIn("vq_missing_support", codes)

    def test_lai_missing_support(self):
        result = consistency.consistency_checker(
            {"LAI": "B"}, "It looked nicer to me overall.")
        codes = [f["code"] for f in result["flags"]]
        self.assertIn("lai_missing_support", codes)

    def test_clean_has_no_flags(self):
        result = consistency.consistency_checker(
            {"VQ": "A", "LAI": "A"},
            "Response A is sharper with more detail and looks more realistic "
            "with fewer artifacts.")
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["severity"], "none")


class TestImageSubjectiveScorer(_Base):
    def _image_response(self, qtype, answer_key):
        q = self.Question.create({
            "name": "Describe",
            "prompt": "Describe the image.",
            "question_type": qtype,
            "subjective_rubric_json": json.dumps(answer_key),
        })
        ev, applicant, assessment = self._evaluator()
        return self.Response.create({
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": q.id,
            "justification": "A fluffy cat sitting on a sofa.",
        })

    def _prompt_response(self):
        return self._image_response("image_prompt", {
            "ideal_prompt": "A fluffy cat sitting on a sofa.",
            "mandatory_elements": ["cat"],
            "penalty_rules": ["no hallucinated objects"],
            "scoring_guide": "Award points for accuracy.",
        })

    def _label_response(self):
        return self._image_response("image_label", {
            "ideal_labels": "A fluffy cat sitting on a sofa.",
            "mandatory_elements": ["cat"],
            "penalty_rules": ["no hallucinated objects"],
            "scoring_guide": "Award points for accuracy.",
        })

    def _v6_payload(self, resp, score, **extra):
        result = {
            "item_id": str(resp.id), "id": resp.id,
            "field_key": "justification", "skills": [],
            "rubric_source": "supplied", "rubric": {}, "gate": "none",
            "reference_answer": "ref", "reasoning": "audit",
            "verdict_consistency": "match", "flags": [],
            "score": score, "passed": score >= 0.70, "feedback": "graded",
        }
        result.update(extra)
        return json.dumps({
            "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
            "submission_flags": [], "results": [result]})

    def test_image_prompt_scorer_scaling(self):
        resp = self._prompt_response()
        fixed = self._v6_payload(resp, 0.80)
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scored = scoring._score_image_prompt_items(self.env, resp)
        self.assertEqual(scored, 1)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_score, 0.8)
        self.assertTrue(resp.llm_passed)
        # EQUAL MARKS: every question worth 1; pass earns the single mark.
        self.assertEqual(resp.llm_max_score, 1)
        self.assertEqual(resp.llm_score, 1)

    def test_image_label_scorer_scaling(self):
        resp = self._label_response()
        fixed = self._v6_payload(resp, 0.80)
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scored = scoring._score_image_label_items(self.env, resp)
        self.assertEqual(scored, 1)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_score, 0.8)
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.llm_max_score, 1)
        self.assertEqual(resp.llm_score, 1)

    def test_image_label_scorer_fail_below_threshold(self):
        resp = self._label_response()
        fixed = self._v6_payload(resp, 0.40)
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scoring._score_image_label_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertFalse(resp.llm_passed)
        self.assertEqual(resp.llm_score, 0)
        self.assertEqual(resp.llm_max_score, 1)

    def test_image_prompt_and_label_use_distinct_rubric_keys(self):
        prompt_item = scoring._build_item(self._prompt_response())
        label_item = scoring._build_item(self._label_response())
        self.assertIn("ideal_prompt", prompt_item)
        self.assertNotIn("ideal_labels", prompt_item)
        self.assertIn("ideal_labels", label_item)
        self.assertNotIn("ideal_prompt", label_item)
        self.assertEqual(prompt_item["candidate_text"],
                         "A fluffy cat sitting on a sofa.")


class TestImageLabelPerBoxRubric(_Base):
    """PHASE 6: image_label gets a per-box rubric synthesised from detections_json
    (one checklist point per detected box, the candidate {number:label} answer
    rendered as readable lines), falling back to the ideal_labels answer key when
    no detections exist."""

    _DETECTIONS = [
        {"number": 1, "label": "car", "description": "a red car",
         "box_px": [20, 15, 80, 60]},
        {"number": 2, "label": "dog", "description": "a brown dog",
         "box_px": [10, 120, 60, 180]},
    ]

    def _label_q(self, detections=None, answer_key=None):
        q = self.Question.create({
            "name": "Label boxes", "prompt": "Identify each numbered box.",
            "question_type": "image_label",
            "subjective_rubric_json":
                json.dumps(answer_key) if answer_key else False})
        self.env["etp.assessment.pro.question.image"].create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "detections_json":
                json.dumps(detections) if detections else False})
        return q

    def _label_resp(self, q, answer):
        ev, applicant, assessment = self._evaluator()
        return self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": answer})

    def test_per_box_rubric_synthesised_from_detections(self):
        q = self._label_q(detections=self._DETECTIONS)
        resp = self._label_resp(q, json.dumps({"1": "car", "2": "dog"}))
        item = scoring._build_item(resp)
        self.assertEqual(item["rubric_source_hint"], "supplied")
        checklist = item["rubric"]["checklist"]
        self.assertEqual(len(checklist), 2)                 # one point per box
        self.assertIn("Box 1", checklist[0])
        self.assertIn("a red car", checklist[0])
        self.assertIn("car", checklist[0])
        # candidate {number:label} answer rendered as readable box lines
        self.assertIn("Box 1: car", item["candidate_text"])
        self.assertIn("Box 2: dog", item["candidate_text"])
        # standing constraints guard hallucinated / skipped labels
        self.assertTrue(any("hallucinated" in c.lower()
                            for c in item["rubric"]["constraints"]))
        self.assertNotIn("ideal_labels", item)              # detections drive it
        self.assertEqual(item["field_key"], "justification")
        self.assertEqual(item["skills"], [])

    def test_falls_back_to_ideal_labels_without_detections(self):
        q = self._label_q(answer_key={
            "ideal_labels": "A red car and a brown dog.",
            "mandatory_elements": ["car", "dog"],
            "penalty_rules": ["no hallucinated objects"],
            "scoring_guide": "Award for accuracy."})
        resp = self._label_resp(q, "A red car and a brown dog.")
        item = scoring._build_item(resp)
        self.assertIn("ideal_labels", item)                 # answer-key fallback
        self.assertNotIn("checklist", item.get("rubric", {}))
        self.assertEqual(item["candidate_text"], "A red car and a brown dog.")

    def test_label_boxes_from_ideal_labels_without_detections(self):
        q = self._label_q(answer_key={
            "ideal_labels": ["search", "play/pause", "share"]})
        ctrl = portal_ctrl.EtpAssessmentPortal()
        _img, label_boxes, _answers = ctrl._image_label_context(q, False)
        self.assertEqual(label_boxes, [1, 2, 3])

    def test_per_box_answer_scored_via_v6_payload(self):
        q = self._label_q(detections=self._DETECTIONS)
        resp = self._label_resp(q, json.dumps({"1": "car", "2": "dog"}))
        payload = json.dumps({
            "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
            "submission_flags": [], "results": [{
                "item_id": str(resp.id), "field_key": "justification",
                "skills": [], "rubric_source": "supplied", "rubric": {},
                "reference_answer": "Box 1: car\nBox 2: dog", "gate": "none",
                "reasoning": "Both boxes correctly identified.",
                "verdict_consistency": "match", "flags": [],
                "score": 0.90, "passed": True, "feedback": "Accurate."}]})
        with patch.object(vertex, "_call_vertex", return_value=payload):
            scoring._score_image_label_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_100, 90.0)      # 0.90 -> 90
        self.assertTrue(resp.llm_passed)
        self.assertEqual(resp.llm_max_score, 1)
        stored = json.loads(resp.llm_result_json)
        self.assertEqual(stored["verdict_consistency"], "match")


class TestImageAbScorerStub(_Base):
    def test_image_ab_scorer_scaling(self):
        q = self._build_image_ab([
            (self.IF, "Response A"),
            (self.OC, "Response A"),
        ])
        ev, applicant, assessment = self._evaluator()
        assessment.require_justification_image_comparison = True  # blend path
        line_vals = [
            (0, 0, {
                "question_dimension_id": qd.id,
                "selected_option_id": self._opt(qd, "Response A").id,
            })
            for qd in q.question_dimension_ids
        ]
        resp = self.Response.create({
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": q.id,
            "justification": "Response A is sharper and more realistic.",
            "line_ids": line_vals,
        })
        fixed = json.dumps({
            "schema_version": "subjective-judge-v6", "pass_threshold": 0.70,
            "submission_flags": [], "results": [{
                "item_id": str(resp.id), "id": resp.id,
                "field_key": "justification", "skills": [],
                "rubric_source": "generated", "rubric": {}, "gate": "none",
                "reference_answer": "A...", "reasoning": "justification aligns",
                "verdict_consistency": "match", "flags": [],
                "score": 0.80, "passed": True, "alignment": "high",
                "strengths": ["clear"], "issues": [], "feedback": "solid",
            }]})
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scoring._score_image_ab_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        # verdicts 100% (2/2) + justification 80 -> ceil(0.75*100 + 0.25*80) = 95
        self.assertEqual(resp.ab_mcq_pct, 100.0)
        self.assertAlmostEqual(resp.llm_raw_score, 0.95)
        self.assertEqual(resp.llm_max_score, 1)


class TestRecordResponse(_Base):
    def test_record_response_image_ab(self):
        q = self._build_image_ab([
            (self.IF, "Response A"),
            (self.VQ, "Response B"),
            (self.LAI, "Response A"),
            (self.OC, "Response A"),
        ])
        ev, _applicant, _assessment = self._evaluator()
        ev.question_order = json.dumps([q.id])
        form = {
            "question_id": str(q.id),
            "justification": "Response A is sharper with more detail.",
        }
        for qd in q.question_dimension_ids:
            form["dimension_%d" % qd.id] = str(
                self._opt(qd, "Response A").id)
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, form)
        self.assertTrue(resp)
        self.assertEqual(len(resp.line_ids), 4)
        self.assertTrue(resp.justification.startswith("Response A"))
        self.assertEqual(resp.state, "submitted")

    def test_record_response_image_prompt(self):
        q = self.Question.create({
            "name": "Prompt it",
            "prompt": "Write a generation prompt.",
            "question_type": "image_prompt",
            "subjective_rubric_json": json.dumps({"ideal_prompt": "x"}),
        })
        ev, _applicant, _assessment = self._evaluator()
        ev.question_order = json.dumps([q.id])
        form = {
            "question_id": str(q.id),
            "justification": "A photorealistic cat on a sofa, soft lighting.",
        }
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, form)
        self.assertTrue(resp)
        self.assertFalse(resp.line_ids)
        self.assertTrue(resp.justification.startswith("A photorealistic"))
        self.assertEqual(resp.state, "submitted")

    def test_record_response_image_label_requires_text(self):
        q = self.Question.create({
            "name": "Label it",
            "prompt": "Label the image.",
            "question_type": "image_label",
        })
        ev, _applicant, _assessment = self._evaluator()
        ev.question_order = json.dumps([q.id])
        form = {"question_id": str(q.id), "justification": "  "}
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, form)
        self.assertFalse(resp)


class TestImageAbBlendScoring(_Base):
    """image_ab = objective verdicts (code) + an optional LLM justification,
    blended ceil(0.75*verdict% + 0.25*justification%). The single mark follows
    the admin-configurable subjective threshold, read live from Settings."""

    OFFICIAL = [("Instruction Following (IF)", "Response A"),
                ("Visual Quality (VQ)", "Response B"),
                ("Label Accuracy (LAI)", "Response A"),
                ("Overall Choice (OC)", "Response A")]

    def _resp(self, assessment, ev, applicant, justification="", correct=3):
        official = list(self.OFFICIAL)
        q = self._build_image_ab(official)
        picks = []
        for i, (name, lbl) in enumerate(official):
            picks.append((name, lbl) if i < correct
                         else (name, "Tie" if lbl != "Tie" else "Both Bad"))
        lines = self._lines_for(q, picks)
        return self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": justification, "line_ids": lines})

    def _decide_at(self, threshold, resp):
        """Set this assessment's subjective threshold and let the compute
        re-decide already-scored answers, then read the fresh values."""
        resp.assessment_id.subjective_threshold = float(threshold)
        resp.invalidate_recordset()

    def test_toggle_off_scores_verdicts_only_immediately(self):
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = False
        r = self._resp(a, ev, applicant, correct=3)
        r._enqueue_subjective_scoring()
        self.assertEqual(r.llm_state, "scored")          # settled at submit, no LLM
        self.assertEqual(r.ab_mcq_pct, 75.0)             # 3 of 4 axes match
        self.assertEqual(r.ab_final_pct, 75.0)           # verdicts only
        self.assertEqual(r.llm_max_score, 1)

    def test_toggle_on_blends_verdicts_and_justification_with_ceil(self):
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = True
        r = self._resp(a, ev, applicant, justification="A is sharper.", correct=3)
        r._enqueue_subjective_scoring()
        self.assertEqual(r.llm_state, "pending")         # justification awaits LLM
        # llm_raw_100 is now the immutable blended final written by the scorer;
        # ab_final_pct mirrors it (the blend is computed in scoring, not re-derived).
        r.write({"llm_raw_100": 80, "llm_state": "scored"})
        self.assertEqual(r.ab_final_pct, 80.0)

    def test_toggle_on_blank_justification_scores_verdicts_only(self):
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = True
        r = self._resp(a, ev, applicant, justification="", correct=3)
        r._enqueue_subjective_scoring()
        self.assertEqual(r.llm_state, "scored")          # blank -> no LLM
        # blank justification -> verdict lane only: llm_raw_100 = verdict% = 75
        self.assertEqual(r.ab_final_pct, 75.0)

    def test_mark_follows_the_configurable_threshold(self):
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = True
        r = self._resp(a, ev, applicant, justification="A is sharper.", correct=3)
        r._enqueue_subjective_scoring()
        r.write({"llm_raw_100": 80, "llm_state": "scored"})
        final = r.ab_final_pct                            # the blend (asserted above)
        self._decide_at(final - 1, r)                     # bar just below the blend
        self.assertEqual(r.llm_score, 1)
        self._decide_at(final + 1, r)                     # bar just above the blend
        self.assertEqual(r.llm_score, 0)

    def test_blank_justification_scored_via_real_submit_path(self):
        # Regression (verifier): the real action_submit path must score a
        # verdicts-only image_ab, not leave it stuck unscored.
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = False
        r = self._resp(a, ev, applicant, justification="", correct=4)
        r.action_submit()
        self.assertEqual(r.state, "submitted")
        self.assertEqual(r.llm_state, "scored")
        self.assertEqual(r.ab_final_pct, 100.0)
        self.assertEqual(r.llm_max_score, 1)

    def test_score_evaluator_skips_vertex_for_verdict_only(self):
        # Regression (verifier): a re-queued verdict-only image_ab is settled
        # WITHOUT a Vertex call.
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = False
        r = self._resp(a, ev, applicant, justification="", correct=3)
        r.write({"llm_state": "pending"})
        with patch.object(vertex, "_call_vertex") as m:
            scoring.score_evaluator(self.env, ev)
        m.assert_not_called()
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "scored")
        self.assertEqual(r.ab_final_pct, 75.0)

    def test_toggle_flip_after_submit_re_decides_and_unsticks_evaluator(self):
        # Review Bug 1 + Risk 2: flipping the toggle after submission must
        # re-decide the answer AND re-derive the evaluator rollup, or the cron
        # would never re-grade an already-'scored' candidate.
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = False
        r = self._resp(a, ev, applicant, justification="A is sharper.", correct=3)
        r.action_submit()                                 # verdict-only -> scored
        ev._compute_subjective_rollup()
        self.assertEqual(r.llm_state, "scored")
        self.assertEqual(ev.llm_state, "scored")          # candidate fully rolled up
        a.write({"require_justification_image_comparison": True})  # now needs LLM
        r.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertEqual(r.llm_state, "pending")          # re-queued for the LLM
        self.assertNotEqual(ev.llm_state, "scored")       # evaluator un-stuck


class TestImageRenderRetry(_Base):
    """Render cron reliability: all-or-nothing storage, 429 re-queue without
    burning an attempt, and a partial-render attempt cap that flips to failed."""

    def _draft(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "RenderP"})
        return self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id,
            "name": "AB draft",
            "question_type": "image_ab",
            "image_brief_json": json.dumps([
                {"slot": "a", "label": "Response A", "prompt": "brief A"},
                {"slot": "b", "label": "Response B", "prompt": "brief B"},
            ]),
            "image_state": "pending",
        })

    def test_full_render_marks_rendered_and_resets_attempts(self):
        d = self._draft()
        imgs = [{"slot": "a", "label": "A", "data": "data:image/png;base64,AAAA"},
                {"slot": "b", "label": "B", "data": "data:image/png;base64,BBBB"}]
        with patch.object(vertex, "render_draft_images", return_value=imgs):
            self.assertTrue(d._render_all_images())
        d.invalidate_recordset()
        self.assertEqual(d.image_state, "rendered")
        self.assertEqual(d.image_render_attempts, 0)

    def test_quota_requeues_without_spending_attempt(self):
        d = self._draft()

        def _boom(*a, **k):
            raise vertex.VertexQuotaError("429 exhausted")

        with patch.object(vertex, "render_draft_images", side_effect=_boom):
            self.assertFalse(d._render_all_images())
        d.invalidate_recordset()
        self.assertEqual(d.image_state, "pending")   # re-queued
        self.assertEqual(d.image_render_attempts, 0)  # NOT spent

    def test_partial_render_spends_attempts_then_fails(self):
        d = self._draft()
        one = [{"slot": "a", "label": "A", "data": "data:image/png;base64,AAAA"}]
        with patch.object(vertex, "render_draft_images", return_value=one):
            self.assertFalse(d._render_all_images())
            d.invalidate_recordset()
            self.assertEqual(d.image_render_attempts, 1)
            self.assertEqual(d.image_state, "pending")
            self.assertFalse(d._render_all_images())
            d.invalidate_recordset()
            self.assertEqual(d.image_render_attempts, 2)
            self.assertEqual(d.image_state, "pending")
            self.assertFalse(d._render_all_images())
            d.invalidate_recordset()
            self.assertEqual(d.image_render_attempts, 3)
            self.assertEqual(d.image_state, "failed")   # cap reached, surfaced


class TestSopDirectGeneration(_Base):
    """Skill-free SOP generation: a SOP document + mocked model produces draft
    questions with no skill link, and an empty prompt refuses."""

    def test_generate_from_sop_creates_drafts_without_skill(self):
        import base64 as _b64
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "SOP P"})
        self.env["etp.assessment.pro.prompt.resource"].create({
            "prompt_id": prompt.id, "name": "sop.pdf",
            "file": _b64.b64encode(b"%PDF-1.4 fake"), "category": "sop"})
        payload = json.dumps([{
            "name": "Q1", "prompt": "What is 2+2?",
            "question_type": "mcq", "difficulty": "easy",
            "options": ["3", "4", "5"], "correct_answer": "4"}])
        with patch.object(vertex, "_call_vertex", return_value=payload):
            draft_ids = vertex.generate_questions_from_sop(self.env, prompt)
        self.assertEqual(len(draft_ids), 1)
        draft = self.env["etp.assessment.pro.prompt.question"].browse(draft_ids)
        self.assertEqual(draft.question_type, "mcq")
        self.assertEqual(draft.question_prompt, "What is 2+2?")

    def test_generate_from_sop_requires_a_source(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "Empty"})
        with self.assertRaises(Exception):
            vertex.generate_questions_from_sop(self.env, prompt)



class TestImagelessQuestionGuards(_Base):
    """A candidate must never get an image question with no image. Two guards:
    approval is blocked until images render, and exam selection drops any image
    question that ended up without its picture(s)."""

    def test_image_ab_requires_both_slots(self):
        q = self._build_image_ab([(self.IF, "Response A"),
                                  (self.OC, "Response A")])
        self.assertTrue(q._has_required_images())        # has a + b
        q.image_ids.filtered(lambda i: i.slot == "b").unlink()
        q.invalidate_recordset()
        self.assertFalse(q._has_required_images())       # missing b

    def test_image_prompt_requires_one_image(self):
        q = self.Question.create({
            "name": "IP", "prompt": "Describe.", "question_type": "image_prompt"})
        self.assertFalse(q._has_required_images())        # no image
        self.env["etp.assessment.pro.question.image"].create({
            "question_id": q.id, "label": "Reference", "slot": "reference"})
        q.invalidate_recordset()
        self.assertTrue(q._has_required_images())

    def test_image_label_requires_one_image(self):
        q = self.Question.create({
            "name": "IL", "prompt": "Label.", "question_type": "image_label"})
        self.assertFalse(q._has_required_images())        # no image
        self.env["etp.assessment.pro.question.image"].create({
            "question_id": q.id, "label": "Image", "slot": "single"})
        q.invalidate_recordset()
        self.assertTrue(q._has_required_images())

    def test_non_image_question_always_ok(self):
        q = self.Question.create({
            "name": "M", "prompt": "Pick.", "question_type": "mcq"})
        self.assertTrue(q._has_required_images())

    def test_approval_blocked_until_images_rendered(self):
        from odoo.exceptions import UserError
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "GP"})
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "AB draft",
            "question_type": "image_ab", "image_state": "pending"})
        with self.assertRaises(UserError):
            draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "draft")            # not approved
        draft.write({
            "images_json": json.dumps([
                {"slot": "a", "label": "A", "data": "data:image/png;base64,AAAA"},
                {"slot": "b", "label": "B", "data": "data:image/png;base64,BBBB"}]),
            "image_state": "rendered"})
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")         # now allowed


class TestImageLabelDetection(_Base):
    """Phase 2 engine: element detection, numbered-box overlay, storage on the
    image row, and the cron that only annotates image_label 'single' images."""

    _BOXES = [
        {"box_2d": [100, 100, 400, 400], "label": "car",
         "description": "a red car"},
        {"box_2d": [500, 500, 700, 700], "label": "car",
         "description": "another car"},
        {"box_2d": [50, 600, 300, 900], "label": "dog",
         "description": "a brown dog"},
    ]

    def _label_image(self, with_image=True, qtype="image_label", slot="single"):
        q = self.Question.create({
            "name": "Label it", "prompt": "Label the image.",
            "question_type": qtype})
        vals = {"question_id": q.id, "label": "Image", "slot": slot}
        if with_image:
            vals["image"] = base64.b64encode(_png_bytes())
        return self.env["etp.assessment.pro.question.image"].create(vals)

    def test_detect_image_elements_parses_and_dedups(self):
        payload = json.dumps(self._BOXES)
        with patch.object(vertex, "_call_vertex", return_value=payload):
            dets = vertex.detect_image_elements(self.env, "Zm9v")
        self.assertEqual([d["label"] for d in dets], ["car", "dog"])  # deduped
        self.assertEqual(dets[0]["box_2d"], [100, 100, 400, 400])
        with patch.object(vertex, "_call_vertex", return_value=payload):
            ui = vertex.detect_image_elements(self.env, "Zm9v", ui=True)
        self.assertEqual(len(ui), 3)                                  # ui keeps all

    def test_annotate_image_returns_png_and_label_key(self):
        png, label_key = imaging.annotate_image(_png_bytes(), self._BOXES)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual([e["number"] for e in label_key], [1, 2, 3])
        self.assertEqual(label_key[0]["label"], "car")
        self.assertEqual(len(label_key[0]["box_px"]), 4)
        left, top, right, bottom = label_key[0]["box_px"]
        self.assertEqual((left, top, right, bottom), (20, 15, 80, 60))  # 0-1000->px

    def test_detect_and_annotate_populates_fields(self):
        img = self._label_image()
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES):
            self.assertTrue(img._detect_and_annotate())
        img.invalidate_recordset()
        self.assertTrue(img.annotated_image)
        key = json.loads(img.detections_json)
        self.assertEqual([e["number"] for e in key], [1, 2, 3])

    def test_detect_and_annotate_noop_without_source_image(self):
        img = self._label_image(with_image=False)
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES) as m:
            self.assertFalse(img._detect_and_annotate())
        m.assert_not_called()
        self.assertFalse(img.detections_json)

    def test_cron_only_processes_image_label(self):
        label_img = self._label_image()
        prompt_img = self._label_image(qtype="image_prompt", slot="reference")
        # The drainer commits per row; commit is forbidden inside a test txn, so
        # no-op it and let the savepoint-scoped writes persist in the test.
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES), \
                patch.object(self.env.cr, "commit", lambda: None):
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        label_img.invalidate_recordset()
        prompt_img.invalidate_recordset()
        self.assertTrue(label_img.detections_json)       # image_label annotated
        self.assertFalse(prompt_img.detections_json)     # image_prompt skipped

    def test_cron_skips_already_detected(self):
        img = self._label_image()
        img.detections_json = json.dumps([{"number": 1, "label": "x"}])
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES) as m:
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        m.assert_not_called()


class TestImageLabelDetectionMode(TestImageLabelDetection):
    """Phase 4: admin-uploaded source images, object-vs-UI detection mode, and
    the on-demand Detect Now button."""

    def test_cron_honors_ui_detection_mode(self):
        img = self._label_image()
        img.question_id.detection_mode = "ui"
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES) as m, \
                patch.object(self.env.cr, "commit", lambda: None):
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        self.assertTrue(m.call_args.kwargs.get("ui"))     # UI prompt selected
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)

    def test_cron_uses_object_mode_by_default(self):
        img = self._label_image()
        self.assertEqual(img.question_id.detection_mode, "object")
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES) as m, \
                patch.object(self.env.cr, "commit", lambda: None):
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        self.assertFalse(m.call_args.kwargs.get("ui"))    # object prompt

    def test_cron_detects_manually_uploaded_image(self):
        # A manager-uploaded single image (no SOP render pipeline) is picked up
        # by the same detection cron.
        img = self._label_image()
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES), \
                patch.object(self.env.cr, "commit", lambda: None):
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)
        self.assertTrue(img.annotated_image)

    def test_action_detect_now_populates_immediately(self):
        img = self._label_image()
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES):
            img.action_detect_now()
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)
        self.assertTrue(img.annotated_image)

    def test_question_action_detect_now_honors_ui_mode(self):
        img = self._label_image()
        img.question_id.detection_mode = "ui"
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES) as m:
            img.question_id.action_detect_now()
        self.assertTrue(m.call_args.kwargs.get("ui"))
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)

    def test_action_detect_now_requires_source_image(self):
        from odoo.exceptions import UserError
        q = self.Question.create({
            "name": "Empty label", "prompt": "Label.",
            "question_type": "image_label"})
        self.env["etp.assessment.pro.question.image"].create({
            "question_id": q.id, "label": "Image", "slot": "single"})
        with self.assertRaises(UserError):
            q.action_detect_now()

    def test_detection_mode_copies_from_draft_on_approve(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "DP"})
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "L draft",
            "question_type": "image_label", "detection_mode": "ui",
            "images_json": json.dumps([
                {"slot": "single", "label": "Image",
                 "data": "data:image/png;base64,AAAA"}]),
            "image_state": "uploaded"})
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        self.assertEqual(draft.approved_question_id.detection_mode, "ui")


class TestImageLabelInlineDetection(_Base):
    """image_label detection runs at RENDER time on the just-rendered in-memory
    bytes, so the reviewer SEES the numbered-box overlay on the DRAFT before
    approving. Approval only CARRIES the already-computed key to the bank image
    (no second detect). A render-time detect failure never rolls back the render
    nor spends the detection budget, leaving the image for the cron to retry."""

    _BOXES = [
        {"box_2d": [100, 100, 400, 400], "label": "car",
         "description": "a red car"},
        {"box_2d": [50, 600, 300, 900], "label": "dog",
         "description": "a brown dog"},
    ]

    def _label_draft(self, detection_mode="object"):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "ILP"})
        return self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "IL draft",
            "question_type": "image_label",
            "question_prompt": "Label the image.",
            "detection_mode": detection_mode,
            "image_brief_json": json.dumps([
                {"slot": "single", "label": "Image",
                 "prompt": "a street with a car and a dog"}]),
            "image_state": "pending"})

    def _render(self, draft, detect=None, side_effect=None):
        rendered = [{"slot": "single", "label": "Image",
                     "data": "data:image/png;base64,%s"
                     % base64.b64encode(_png_bytes()).decode()}]
        det_kw = ({"side_effect": side_effect} if side_effect is not None
                  else {"return_value": detect if detect is not None
                        else self._BOXES})
        with patch.object(vertex, "render_draft_images",
                          return_value=rendered), \
                patch.object(vertex, "detect_image_elements",
                             **det_kw) as m:
            ok = draft._render_all_images()
        draft.invalidate_recordset()
        return ok, m

    def _draft_spec(self, draft):
        return json.loads(draft.images_json)[0]

    def _single_image(self, draft):
        img = draft.approved_question_id.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        img.invalidate_recordset()
        return img

    def test_detection_runs_at_render_time_on_draft(self):
        draft = self._label_draft()
        ok, m = self._render(draft)
        self.assertTrue(ok)
        self.assertEqual(draft.image_state, "rendered")
        spec = self._draft_spec(draft)
        key = json.loads(spec["detections_json"])
        self.assertEqual([e["number"] for e in key], [1, 2])   # boxes on draft
        self.assertTrue(
            spec["annotated_data"].startswith("data:image/png;base64,"))
        m.assert_called_once()
        self.assertFalse(m.call_args.kwargs.get("ui"))         # object mode

    def test_render_time_detection_honors_ui_detection_mode(self):
        draft = self._label_draft(detection_mode="ui")
        _ok, m = self._render(draft)
        self.assertTrue(m.call_args.kwargs.get("ui"))

    def test_draft_preview_shows_annotated_overlay(self):
        draft = self._label_draft()
        self._render(draft)
        self.assertIn("data:image/png;base64,", draft.image_preview or "")

    def _source_url_dense_draft(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "ILP"})
        dense = [{"number": 1, "box_2d": [40, 30, 90, 300], "label": "Search",
                  "description": "Focuses the search field"},
                 {"number": 2, "box_2d": [40, 820, 90, 980], "label": "Cart",
                  "description": "Opens the cart"}]
        return self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "IL src draft",
            "question_type": "image_label",
            "question_prompt": "Label every control.",
            "source_url": "https://github.com",
            "label_boxes_json": json.dumps(dense),
            "behavioural_key_json": json.dumps([
                {"number": 1, "element": "Search",
                 "functionality": "Focuses the search field"},
                {"number": 2, "element": "Cart",
                 "functionality": "Opens the cart"}]),
            "image_brief_json": json.dumps([
                {"slot": "single", "label": "Screenshot",
                 "prompt": "a page with a search box and a cart"}]),
            "image_state": "pending"})

    def test_source_url_draft_preview_boxed_from_dense_map_without_detect(self):
        # A source_url image whose live DOM capture is unavailable (Playwright off)
        # falls back to the stored dense map for the ADMIN PREVIEW only - this is
        # the source_url branch, unchanged by the label-position fix (which governs
        # SYNTHETIC images). Patch Playwright off so the test is deterministic and
        # never reaches the real page.
        draft = self._source_url_dense_draft()
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False):
            ok, m = self._render(draft)
        self.assertTrue(ok)
        m.assert_not_called()                    # dense map -> ZERO Vertex detect
        spec = self._draft_spec(draft)
        key = json.loads(spec["detections_json"])
        self.assertEqual([e["number"] for e in key], [1, 2])
        self.assertTrue(
            spec["annotated_data"].startswith("data:image/png;base64,"))
        self.assertIn("data:image/png;base64,", draft.image_preview or "")

    def test_source_url_dense_preview_does_not_leak_key_to_bank_image(self):
        draft = self._source_url_dense_draft()
        self._render(draft)
        with patch.object(vertex, "detect_image_elements") as m:
            draft.action_approve()
        m.assert_not_called()
        img = self._single_image(draft)
        self.assertEqual(img.source_url, "https://github.com")
        self.assertFalse(img.detections_json)    # live capture still owns the key

    def test_approval_carries_key_without_redetecting(self):
        draft = self._label_draft()
        self._render(draft)
        with patch.object(vertex, "detect_image_elements") as m:
            draft.action_approve()
        m.assert_not_called()                            # no second detect
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        img = self._single_image(draft)
        key = json.loads(img.detections_json)
        self.assertEqual([e["number"] for e in key], [1, 2])   # carried
        self.assertTrue(img.annotated_image)                   # overlay carried

    def test_render_failure_keeps_render_and_spares_budget(self):
        draft = self._label_draft()

        def _boom(*a, **k):
            raise RuntimeError("vertex exploded")

        ok, _m = self._render(draft, side_effect=_boom)
        self.assertTrue(ok)                              # render committed
        self.assertEqual(draft.image_state, "rendered")
        spec = self._draft_spec(draft)
        self.assertNotIn("detections_json", spec)        # no key at render time
        with patch.object(vertex, "detect_image_elements", side_effect=_boom):
            draft.action_approve()                       # fallback must NOT raise
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")        # render/approve kept
        img = self._single_image(draft)
        self.assertFalse(img.detections_json)            # still no key
        self.assertEqual(img.detection_attempts, 0)      # cron budget untouched
        self.assertTrue(img.image or img.image_url)      # source kept for retry
        with patch.object(vertex, "detect_image_elements",
                          return_value=self._BOXES), \
                patch.object(self.env.cr, "commit", lambda: None):
            self.env["etp.assessment.pro.question.image"]\
                ._cron_detect_image_labels()
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)             # cron retried from store


class TestImagePromptTwoImage(_Base):
    """image_prompt renders BOTH a reference and an output image for a
    transform task (the old code took the first brief and dropped the rest),
    while a from-scratch single-image image_prompt still renders exactly one."""

    def _draft_fields(self, images, answer_key=None):
        item = {"name": "IP", "prompt": "Write the transformation prompt.",
                "question_type": "image_prompt",
                "image_specs": {"images": images,
                                "answer_key": answer_key or {
                                    "ideal_prompt": "turn the sketch into a "
                                    "photorealistic render"}}}
        return vertex._build_image_draft_fields(self.env, "image_prompt", item)

    def test_reference_output_pair_renders_two_briefs(self):
        vals = self._draft_fields([
            {"slot": "reference", "label": "Reference",
             "prompt": "a pencil sketch of a car"},
            {"slot": "output", "label": "Output",
             "prompt": "a photorealistic red car"},
        ])
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual(len(briefs), 2)                     # neither dropped
        self.assertEqual([b["slot"] for b in briefs], ["reference", "output"])
        self.assertEqual(briefs[0]["prompt"], "a pencil sketch of a car")
        self.assertEqual(briefs[1]["prompt"], "a photorealistic red car")
        key = json.loads(vals["rubric_json"])
        self.assertTrue(key.get("ideal_prompt"))

    def test_unslotted_pair_maps_reference_then_output(self):
        vals = self._draft_fields([
            {"prompt": "the before image"},
            {"prompt": "the after image"},
        ])
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual([b["slot"] for b in briefs], ["reference", "output"])

    def test_output_first_is_reordered_reference_before_output(self):
        vals = self._draft_fields([
            {"slot": "output", "prompt": "the result"},
            {"slot": "reference", "prompt": "the source"},
        ])
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual([b["slot"] for b in briefs], ["reference", "output"])
        self.assertEqual(briefs[0]["prompt"], "the source")

    def test_single_image_still_renders_one_single_slot(self):
        vals = self._draft_fields([
            {"slot": "single", "label": "Image",
             "prompt": "a lone sunset over water"}])
        briefs = json.loads(vals["image_brief_json"])
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["slot"], "single")

    def test_two_image_form_passes_validation(self):
        item = {"name": "IP", "prompt": "Write it.",
                "question_type": "image_prompt",
                "image_specs": {
                    "images": [
                        {"slot": "reference", "prompt": "before"},
                        {"slot": "output", "prompt": "after"}],
                    "answer_key": {"ideal_prompt": "make it shiny"}}}
        self.assertEqual(
            vertex._validate_question_item(item, "image_prompt"), [])

    def test_render_and_approve_creates_both_image_records(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "IPP"})
        vals = self._draft_fields([
            {"slot": "reference", "prompt": "a pencil sketch of a car"},
            {"slot": "output", "prompt": "a photorealistic red car"},
        ])
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "IP draft",
            "question_type": "image_prompt",
            "question_prompt": "Write the transformation prompt.",
            "rubric_json": vals["rubric_json"],
            "image_brief_json": vals["image_brief_json"],
            "image_state": "pending"})
        rendered = [
            {"slot": "reference", "label": "Reference",
             "data": "data:image/png;base64,RRRR"},
            {"slot": "output", "label": "Output",
             "data": "data:image/png;base64,OOOO"}]
        with patch.object(vertex, "render_draft_images", return_value=rendered):
            self.assertTrue(draft._render_all_images())
        draft.invalidate_recordset()
        self.assertEqual(draft.image_state, "rendered")
        draft.action_approve()
        draft.invalidate_recordset()
        q = draft.approved_question_id
        self.assertEqual(len(q.image_ids), 2)                 # both persisted
        self.assertEqual(set(q.image_ids.mapped("slot")),
                         {"reference", "output"})
        self.assertTrue(q._has_required_images())


class TestImageLabelSourceUrlCapture(_Base):
    """image_label REAL-PAGE CAPTURE (primary) + synthetic hybrid fallback: a
    source_url draft persists the capture directives, approval carries them to
    the 'single' bank image, the detect path drives a live DOM capture threading
    dismiss/wait_ms, and an unavailable / failed / zero-box capture degrades to
    the synthetic render+detect path. No real network / Vertex / Playwright."""

    _BOXES = [
        {"box_2d": [100, 100, 400, 400], "label": "car",
         "description": "a red car"},
        {"box_2d": [50, 600, 300, 900], "label": "dog",
         "description": "a brown dog"},
    ]

    def _source_item(self):
        return {
            "name": "Label GH", "prompt": "Label every control.",
            "question_type": "image_label",
            "image_specs": {
                "source_url": "https://github.com",
                "application": "GitHub",
                "viewport": {"width": 1440, "height": 900},
                "wait_ms": 3000,
                "dismiss": [".cookie-accept", "#onetrust-accept-btn-handler"],
                "coverage_expected": "yes",
                "images": [{"slot": "single", "label": "Screenshot",
                            "prompt": "a synthetic GitHub homepage screenshot"}],
                "answer_key": {"ideal_labels": "the GitHub homepage controls"}}}

    def _capture_result(self, boxes=True):
        manifest = [{"number": 1, "tag": "a", "role": "", "name": "Search",
                     "text": "Search", "href": "https://github.com/search",
                     "box_css": [10, 10, 90, 40], "in_shadow": False,
                     "boxed_via_label": False}] if boxes else []
        key = [{"number": 1, "element": "Search",
                "functionality": "Opens Search"}] if boxes else []
        label_key = [{"number": 1, "label": "Search",
                      "description": "Opens Search",
                      "box_px": [10, 10, 90, 40]}] if boxes else []
        return {
            "screenshot_png": _png_bytes(), "annotated_png": _png_bytes(),
            "dom_manifest": manifest, "behavioural_key": key,
            "label_key": label_key, "omitted_element": None,
            "coverage_expected": "yes"}

    def _capture_image(self, config=True, image=True):
        q = self.Question.create({
            "name": "Cap", "prompt": "Label.", "question_type": "image_label"})
        vals = {"question_id": q.id, "label": "Image", "slot": "single",
                "source_url": "https://github.com"}
        if image:
            vals["image"] = base64.b64encode(_png_bytes())
        if config:
            vals["capture_config_json"] = json.dumps({
                "viewport": {"width": 1440, "height": 900}, "wait_ms": 3000,
                "dismiss": [".cookie-accept"]})
        return self.env["etp.assessment.pro.question.image"].create(vals)

    def test_draft_fields_persist_source_url_and_capture_config(self):
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", self._source_item())
        self.assertEqual(vals["source_url"], "https://github.com")
        cfg = json.loads(vals["capture_config_json"])
        self.assertEqual(cfg["viewport"], {"width": 1440, "height": 900})
        self.assertEqual(cfg["wait_ms"], 3000)
        self.assertIn(".cookie-accept", cfg["dismiss"])
        self.assertEqual(vals["coverage_expected"], "yes")
        self.assertEqual(vals["label_application"], "GitHub")
        briefs = json.loads(vals["image_brief_json"])   # synthetic fallback kept
        self.assertTrue(briefs and briefs[0]["prompt"])

    def test_validation_accepts_source_url_only(self):
        item = {"name": "x", "prompt": "p", "question_type": "image_label",
                "image_specs": {"source_url": "https://www.wikipedia.org"}}
        self.assertEqual(
            vertex._validate_question_item(item, "image_label"), [])

    def _synthetic_spotify_item(self):
        """A synthetic-only image_label (fake Spotify UI, NO source_url) — the
        exact shape the live bug produced."""
        return {
            "name": "Label Spotify", "prompt": "Label every control.",
            "question_type": "image_label",
            "image_specs": {
                "application": "Spotify",
                "images": [{"slot": "single", "label": "Screenshot",
                            "prompt": "a synthetic Spotify player UI"}],
                "answer_key": {"ideal_labels": "the Spotify player controls"}}}

    def test_missing_source_url_is_repaired_from_application(self):
        item = self._synthetic_spotify_item()
        # the mandatory-source_url contract accepts it (a real URL is derivable)
        self.assertEqual(
            vertex._validate_question_item(item, "image_label"), [])
        vals = vertex._build_image_draft_fields(self.env, "image_label", item)
        # persisted draft now carries a REAL public URL, not a synthetic-only draft
        self.assertEqual(vals["source_url"], "https://open.spotify.com")
        self.assertEqual(vals["coverage_expected"], "yes")
        briefs = json.loads(vals["image_brief_json"])   # synthetic kept as fallback
        self.assertTrue(briefs and briefs[0]["prompt"])

    def test_synthetic_only_without_answer_key_is_rejected(self):
        # unknown app, no source_url, brief but NO answer key/boxes -> rejected
        item = {"name": "x", "prompt": "p", "question_type": "image_label",
                "image_specs": {
                    "application": "SomeInternalTool",
                    "images": [{"slot": "single", "prompt": "a made-up UI"}]}}
        errs = vertex._validate_question_item(item, "image_label")
        self.assertTrue(errs)
        vals = vertex._build_image_draft_fields(self.env, "image_label", item)
        self.assertFalse(vals.get("source_url"))   # nothing derivable, no url

    def test_approve_carries_capture_directives_to_single_image(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "SU"})
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", self._source_item())
        png = base64.b64encode(_png_bytes()).decode()
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "SU draft",
            "question_type": "image_label",
            "question_prompt": "Label every control.",
            "source_url": vals["source_url"],
            "capture_config_json": vals["capture_config_json"],
            "coverage_expected": vals["coverage_expected"],
            "label_application": vals["label_application"],
            "image_brief_json": vals["image_brief_json"],
            "images_json": json.dumps([
                {"slot": "single", "label": "Screenshot",
                 "data": "data:image/png;base64,%s" % png}]),
            "image_state": "rendered"})
        draft.action_approve()
        draft.invalidate_recordset()
        self.assertEqual(draft.state, "approved")
        img = draft.approved_question_id.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        self.assertEqual(img.source_url, "https://github.com")
        self.assertTrue(img.capture_config_json)
        self.assertEqual(img.coverage_expected, "yes")
        self.assertEqual(img.label_application, "GitHub")
        self.assertFalse(img.detections_json)   # left keyless for the capture path

    def test_capture_yields_real_page_detections(self):
        img = self._capture_image()
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             return_value=self._capture_result()):
            self.assertTrue(img._detect_and_annotate())
        img.invalidate_recordset()
        self.assertTrue(img.annotated_image)
        key = json.loads(img.detections_json)
        self.assertEqual(key[0]["label"], "Search")
        self.assertTrue(img.dom_manifest_json)
        self.assertTrue(img.behavioural_key_json)

    def test_capture_makes_zero_image_model_calls(self):
        # a source_url row must NEVER render (gemini-3-pro-image) or detect
        img = self._capture_image()
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             return_value=self._capture_result()), \
                patch.object(vertex, "generate_image") as gen, \
                patch.object(vertex, "detect_image_elements") as det:
            self.assertTrue(img._detect_and_annotate())
        gen.assert_not_called()          # no image-model render
        det.assert_not_called()          # no Gemini box-detection
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)   # boxes came from the DOM capture

    def test_dismiss_and_wait_ms_threaded_into_capture(self):
        img = self._capture_image()
        spy = MagicMock(return_value=self._capture_result())
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate", spy):
            img._detect_and_annotate()
        self.assertEqual(spy.call_args.args[0], "https://github.com")
        kwargs = spy.call_args.kwargs
        self.assertEqual(kwargs["wait_ms"], 3000)
        self.assertEqual(kwargs["dismiss"], [".cookie-accept"])
        self.assertEqual(kwargs["viewport"], (1440, 900))

    def test_capture_unavailable_falls_back_to_synthetic(self):
        img = self._capture_image()
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False), \
                patch.object(vertex, "detect_image_elements",
                             return_value=self._BOXES) as m:
            self.assertTrue(img._detect_and_annotate())
        m.assert_called_once()                       # synthetic render+detect ran
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)
        self.assertTrue(img.annotated_image)

    def test_capture_zero_boxes_falls_back_to_synthetic(self):
        img = self._capture_image()
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             return_value=self._capture_result(boxes=False)), \
                patch.object(vertex, "detect_image_elements",
                             return_value=self._BOXES) as m:
            self.assertTrue(img._detect_and_annotate())
        m.assert_called_once()                       # zero DOM boxes -> synthetic
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)

    def test_capture_error_falls_back_to_synthetic(self):
        img = self._capture_image()

        def _boom(*a, **k):
            raise RuntimeError("net unreachable")

        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             side_effect=_boom), \
                patch.object(vertex, "detect_image_elements",
                             return_value=self._BOXES) as m:
            self.assertTrue(img._detect_and_annotate())
        m.assert_called_once()
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)

    def test_offloaded_s3_image_fetched_via_authenticated_download(self):
        P = self.env["ir.config_parameter"].sudo()
        P.set_param("etp_assessment_pro.s3_bucket", "mybucket")
        P.set_param("etp_assessment_pro.s3_region", "us-east-1")
        P.set_param("etp_assessment_pro.s3_access_key_id", "AK")
        P.set_param("etp_assessment_pro.s3_secret_key", "SK")
        q = self.Question.create({
            "name": "S3", "prompt": "Label.", "question_type": "image_label"})
        img = self.env["etp.assessment.pro.question.image"].create({
            "question_id": q.id, "label": "Image", "slot": "single",
            "image_url":
                "https://mybucket.s3.us-east-1.amazonaws.com/etp/foo.png"})
        png = _png_bytes()
        with patch.object(s3_service, "download",
                          return_value=(png, "image/png")) as dl, \
                patch.object(image_ingest, "_download") as pub:
            raw = img._source_image_bytes()
        dl.assert_called_once()                      # authenticated S3 GET
        pub.assert_not_called()                      # NOT an unsigned public GET
        self.assertEqual(raw, png)

    _DENSE_BOXES = [
        {"number": 1, "box_2d": [40, 30, 90, 300], "label": "Search",
         "description": "Focuses the search field"},
        {"number": 2, "box_2d": [40, 820, 90, 980], "label": "Cart",
         "description": "Opens the cart"},
    ]

    def _capture_image_with_dense_map(self):
        img = self._capture_image()
        img.write({"label_boxes_json": json.dumps(self._DENSE_BOXES)})
        return img

    def _source_item_dense(self):
        item = self._source_item()
        item["image_specs"]["boxes"] = [
            {"number": 1, "box_2d": [40, 30, 90, 300], "element": "Search",
             "functionality": "Focuses the search field"},
            {"number": 2, "box_2d": [40, 820, 90, 980], "element": "Cart",
             "functionality": "Opens the cart"}]
        item["image_specs"]["answer_key"] = {
            "ideal_labels": {"1": "Focuses the search field",
                             "2": "Opens the cart"}}
        return item

    def test_fallback_detects_after_render_not_from_dense_map(self):
        # CORRECTED CONTRACT (label-position fix): the generator's dense map holds
        # coordinates the TEXT model guessed BEFORE the screenshot was rendered, so
        # they never align with the image the IMAGE model actually drew - the cause
        # of "labels at the wrong positions". When no live DOM capture is possible,
        # we now DETECT on the actual rendered pixels (research renderers/ui.py),
        # never draw the guessed boxes. So detection MUST run.
        img = self._capture_image_with_dense_map()
        fake_dets = [
            {"box_2d": [40, 30, 90, 300], "label": "Search",
             "description": "Focuses the search field"},
            {"box_2d": [40, 820, 90, 980], "label": "Cart",
             "description": "Opens the cart"}]
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False), \
                patch.object(vertex, "detect_image_elements",
                             return_value=fake_dets) as det:
            self.assertTrue(img._detect_and_annotate())
        det.assert_called_once()                      # detect-after-render, not dense map
        img.invalidate_recordset()
        key = json.loads(img.detections_json)
        self.assertEqual([e["number"] for e in key], [1, 2])
        self.assertEqual(key[0]["label"], "Search")
        self.assertTrue(img.annotated_image)

    def test_capture_error_falls_back_to_detect_after_render(self):
        img = self._capture_image_with_dense_map()

        def _boom(*a, **k):
            raise RuntimeError("net unreachable")

        fake_dets = [
            {"box_2d": [40, 30, 90, 300], "label": "Search",
             "description": "Focuses the search field"}]
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(dom_capture, "capture_and_annotate",
                             side_effect=_boom), \
                patch.object(vertex, "detect_image_elements",
                             return_value=fake_dets) as det:
            self.assertTrue(img._detect_and_annotate())
        det.assert_called_once()                      # detect-after-render covers the failure
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)
        self.assertTrue(img.annotated_image)

    def test_build_source_url_item_carries_dense_fallback_map(self):
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", self._source_item_dense())
        self.assertEqual(vals["source_url"], "https://github.com")
        geometry = json.loads(vals["label_boxes_json"])
        self.assertEqual(geometry[0]["box_2d"], [40, 30, 90, 300])
        self.assertTrue(vals["behavioural_key_json"])

    def test_approve_carries_dense_map_then_detects_after_render(self):
        prompt = self.env["etp.assessment.pro.prompt"].create({"name": "SUD"})
        vals = vertex._build_image_draft_fields(
            self.env, "image_label", self._source_item_dense())
        png = base64.b64encode(_png_bytes()).decode()
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": prompt.id, "name": "SUD draft",
            "question_type": "image_label",
            "question_prompt": "Label every control.",
            "source_url": vals["source_url"],
            "capture_config_json": vals.get("capture_config_json") or False,
            "coverage_expected": vals["coverage_expected"],
            "label_application": vals["label_application"],
            "behavioural_key_json": vals["behavioural_key_json"],
            "label_boxes_json": vals["label_boxes_json"],
            "image_brief_json": vals["image_brief_json"],
            "images_json": json.dumps([
                {"slot": "single", "label": "Screenshot",
                 "data": "data:image/png;base64,%s" % png}]),
            "image_state": "rendered"})
        draft.action_approve()
        draft.invalidate_recordset()
        img = draft.approved_question_id.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        self.assertTrue(img.label_boxes_json)        # dense fallback map carried
        self.assertFalse(img.detections_json)        # capture-primary still runs
        # CORRECTED CONTRACT: with no live DOM capture available, the box geometry
        # comes from DETECTION on the actual rendered pixels - not the generator's
        # guessed dense map - so the labels land where the elements really are.
        fake_dets = [
            {"box_2d": [40, 30, 90, 300], "label": "Search",
             "description": "Focuses the search field"},
            {"box_2d": [40, 820, 90, 980], "label": "Cart",
             "description": "Opens the cart"}]
        with patch.object(dom_capture, "PLAYWRIGHT_AVAILABLE", False), \
                patch.object(vertex, "detect_image_elements",
                             return_value=fake_dets) as det:
            self.assertTrue(img._detect_and_annotate())
        det.assert_called_once()
        img.invalidate_recordset()
        self.assertTrue(img.detections_json)
        self.assertTrue(img.annotated_image)
