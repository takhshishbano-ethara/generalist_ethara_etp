import base64
import io
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import (
    vertex, scoring, consistency, imaging)
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
