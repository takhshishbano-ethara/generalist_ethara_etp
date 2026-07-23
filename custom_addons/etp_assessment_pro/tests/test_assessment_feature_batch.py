# -*- coding: utf-8 -*-
"""Regression tests for the assessment-screen feature batch:

  #1 candidate_link  - per-candidate portal URL exposed on the evaluator.
  #3 raw_result      - secondary score: objective 0/1 + subjective raw (0-1).
  #4 assessment chatter - etp.assessment.pro logs user actions to mail.thread.
  #5 zoomable draft image - prompt.image_preview marks thumbnails zoomable.

All DB-level and mock-scored (no live Vertex): raw scores are written straight
to llm_raw_100 + llm_state, the same immutable path the grader uses.
"""
import json

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("-at_install", "post_install")
class TestAssessmentFeatureBatch(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    def _fixture(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        gen = self.env["etp.assessment.pro.prompt"].create({"name": "Gen"})
        assessment = self.Assessment.create({
            "name": "Batch Assessment", "generator_id": gen.id})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
        })
        return ev, applicant, assessment

    def _mcq(self, name="MCQ"):
        q = self.Question.create({
            "name": name, "prompt": "Pick one.", "question_type": "mcq"})
        self.QDim.create({
            "question_id": q.id, "name": "axis",
            "option_line_ids": [
                (0, 0, {"name": "Right", "sequence": 10, "is_correct": True}),
                (0, 0, {"name": "Wrong", "sequence": 20, "is_correct": False}),
            ],
        })
        return q

    def _right_option(self, q):
        qd = q.question_dimension_ids[:1]
        return qd.option_line_ids.filtered("is_correct")[:1], qd

    # ---------------------------------------------------------------- #1
    def test_candidate_link_uses_base_url_and_token(self):
        self.ICP.set_param("web.base.url", "https://exam.example.com")
        ev, _app, _ass = self._fixture()
        ev.access_token = "tok-abc-123"
        ev.invalidate_recordset(["candidate_link"])
        self.assertEqual(
            ev.candidate_link,
            "https://exam.example.com/pro_assessment/tok-abc-123")

    def test_candidate_link_blank_without_token(self):
        ev, _app, _ass = self._fixture()
        ev.access_token = False
        ev.invalidate_recordset(["candidate_link"])
        self.assertFalse(ev.candidate_link)

    # ---------------------------------------------------------------- #3
    def test_raw_result_objective_plus_subjective_fraction(self):
        """One correct MCQ (1.0) + one subjective raw 73/100 (0.73) = 1.73."""
        ev, app, ass = self._fixture()
        # objective: correct MCQ -> score 1
        q_obj = self._mcq("Obj")
        right, qd = self._right_option(q_obj)
        r_obj = self.Response.create({
            "assessment_id": ass.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": app.id, "question_id": q_obj.id,
            "state": "submitted",
            "line_ids": [(0, 0, {
                "question_dimension_id": qd.id,
                "selected_option_id": right.id})],
        })
        # subjective: raw 73 -> fraction 0.73
        q_sub = self.Question.create({
            "name": "Sub", "prompt": "Explain.",
            "question_type": "subjective_rubric"})
        r_sub = self.Response.create({
            "assessment_id": ass.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": app.id, "question_id": q_sub.id,
            "justification": "A is better because of crisp edges.",
            "state": "submitted",
        })
        r_sub.write({"llm_raw_100": 73.0, "llm_state": "scored"})
        r_obj.invalidate_recordset()
        r_sub.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertEqual(r_obj.score, 1)
        self.assertAlmostEqual(r_sub.llm_raw_score, 0.73)
        # objective 1.0 + subjective 0.73 = 1.73
        self.assertAlmostEqual(ev.raw_result, 1.73)

    def test_raw_result_failing_subjective_still_counts_raw(self):
        """A subjective answer that FAILS the 0/1 threshold still adds its raw
        fraction to raw_result (that is the whole point of the secondary score)."""
        ev, app, ass = self._fixture()
        q_sub = self.Question.create({
            "name": "Sub", "prompt": "Explain.",
            "question_type": "subjective_rubric"})
        r_sub = self.Response.create({
            "assessment_id": ass.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": app.id, "question_id": q_sub.id,
            "justification": "Weak but on-topic.",
            "state": "submitted",
        })
        # 40/100 is below the default 70 threshold -> llm_score 0, but raw 0.40
        r_sub.write({"llm_raw_100": 40.0, "llm_state": "scored"})
        r_sub.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertEqual(r_sub.llm_score, 0)          # fails the 0/1 mark
        self.assertAlmostEqual(ev.raw_result, 0.40)   # raw fraction still counts

    def test_raw_result_ignores_unscored_and_unsubmitted(self):
        ev, app, ass = self._fixture()
        q_sub = self.Question.create({
            "name": "Sub", "prompt": "Explain.",
            "question_type": "subjective_rubric"})
        # submitted but NOT yet scored -> contributes 0
        self.Response.create({
            "assessment_id": ass.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": app.id, "question_id": q_sub.id,
            "justification": "Pending grade.", "state": "submitted",
        })
        ev.invalidate_recordset()
        self.assertAlmostEqual(ev.raw_result, 0.0)

    # ---------------------------------------------------------------- #4
    def test_assessment_inherits_mail_thread(self):
        self.assertIn("mail.thread", self.Assessment._inherit)
        _ev, _app, ass = self._fixture()
        self.assertTrue(hasattr(ass, "message_post"))

    def test_action_cancel_logs_to_chatter(self):
        _ev, _app, ass = self._fixture()
        before = len(ass.message_ids)
        ass.action_cancel()
        ass.invalidate_recordset(["message_ids"])
        self.assertGreater(len(ass.message_ids), before)
        self.assertTrue(any("Cancelled" in (m.body or "")
                            for m in ass.message_ids))

    def test_log_activity_is_best_effort(self):
        """_log_activity must never raise even on a fresh/edge record."""
        _ev, _app, ass = self._fixture()
        # should not raise
        ass._log_activity("plain audit line")
        ass.invalidate_recordset(["message_ids"])
        self.assertTrue(any("plain audit line" in (m.body or "")
                            for m in ass.message_ids))

    # ---------------------------------------------------------------- #5
    def test_draft_image_preview_marks_zoomable(self):
        """A draft question with a rendered image emits an .etp-image-zoomable
        wrapper so the backend lightbox can bind it (parity with the candidate
        portal preview)."""
        gen = self.env["etp.assessment.pro.prompt"].create({"name": "ImgGen"})
        draft = self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": gen.id,
            "name": "Img",
            "question_prompt": "Label it.",
            "question_type": "image_label",
            "images_json": json.dumps([
                {"slot": "single",
                 "url": "/web/image/etp/1.png",
                 "label": "Screenshot"}]),
        })
        draft.invalidate_recordset(["image_preview", "has_images"])
        self.assertTrue(draft.has_images)
        html = draft.image_preview or ""
        self.assertIn("etp-image-zoomable", html)
        self.assertIn('tabindex="0"', html)
        self.assertIn("/web/image/etp/1.png", html)


@tagged("-at_install", "post_install")
class TestScoreDisplayAndNoGrade(TransactionCase):
    """Score-presentation follow-ups (candidate + admin clarity pass):

      - raw_result keeps 3-decimal precision (0.667 style, not 66.7 / 0.67).
      - the candidate per-question review row surfaces the subjective RAW
        score as a 0-1 decimal, not a 0-100 percentage.
      - an auto-submitted (time-expired) placeholder answer reads 'No Grade'
        on the review page rather than the misleading 'Awaiting grading'.
    """

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]

    def _fixture(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        gen = self.env["etp.assessment.pro.prompt"].create({"name": "Gen"})
        assessment = self.Assessment.create({
            "name": "Score Assessment", "generator_id": gen.id})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id, "applicant_id": applicant.id})
        return ev, applicant, assessment

    def _subjective(self, ass, ev, app, raw_100, state="scored",
                    auto=False, justification="An answer."):
        q = self.Question.create({
            "name": "Sub", "prompt": "Explain.",
            "question_type": "subjective_rubric"})
        r = self.Response.create({
            "assessment_id": ass.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": app.id, "question_id": q.id,
            "justification": justification, "state": "submitted",
            "auto_submitted": auto,
        })
        if state == "scored":
            r.write({"llm_raw_100": raw_100, "llm_state": "scored"})
        r.invalidate_recordset()
        return q, r

    def test_raw_result_keeps_three_decimals(self):
        """2/3 correct -> 0.667, NOT 0.67 or 66.7 (the reported bug)."""
        ev, app, ass = self._fixture()
        self._subjective(ass, ev, app, 66.66667)
        ev.invalidate_recordset()
        # 66.667/100 -> 0.66667 raw, stored rounded to 3dp = 0.667
        self.assertEqual(round(ev.raw_result, 3), 0.667)

    def _review_rows(self, ev):
        """Call the portal review-row builder with a minimal request context.

        _build_answer_review reads request.env; bind a lightweight stand-in so
        the pure row-shaping logic (verdict / score_raw) is unit-testable
        without spinning up a real HTTP request.
        """
        from odoo.addons.etp_assessment_pro.controllers import portal as _portal

        class _Req:
            env = self.env
        original = _portal.request
        _portal.request = _Req()
        try:
            return _portal.EtpAssessmentPortal()._build_answer_review(ev)
        finally:
            _portal.request = original

    def test_review_row_subjective_score_is_raw_decimal(self):
        """The candidate review row carries score_raw as a 0-1 fraction
        (e.g. 0.73), never a 0-100 percentage, mirroring the admin Raw Score."""
        ev, app, ass = self._fixture()
        q, r = self._subjective(ass, ev, app, 73.0)
        ev.question_order = json.dumps([q.id])
        ev.invalidate_recordset()
        rows = self._review_rows(ev)
        row = next(x for x in rows if x["type"] == "subjective_rubric")
        # 0-1 fraction, not 73
        self.assertAlmostEqual(row["score_raw"], 0.73)
        self.assertLessEqual(row["score_raw"], 1.0)

    def test_review_row_auto_submitted_reads_no_grade(self):
        """A time-expired auto-submitted placeholder is never graded, so the
        review row says 'No Grade' rather than 'Awaiting grading'."""
        ev, app, ass = self._fixture()
        q, r = self._subjective(
            ass, ev, app, 0.0, state="pending", auto=True,
            justification="[Auto-submitted: time expired]")
        ev.question_order = json.dumps([q.id])
        ev.invalidate_recordset()
        rows = self._review_rows(ev)
        row = next(x for x in rows if x["type"] == "subjective_rubric")
        self.assertEqual(row["verdict"], "No Grade")
        self.assertIsNone(row["score_raw"])

    def test_review_row_pending_still_awaiting(self):
        """A genuinely pending (not auto-submitted) subjective answer still
        reads 'Awaiting grading' - the No-Grade path must not swallow it."""
        ev, app, ass = self._fixture()
        q, r = self._subjective(
            ass, ev, app, 0.0, state="pending", auto=False,
            justification="Real answer awaiting the grader.")
        ev.question_order = json.dumps([q.id])
        ev.invalidate_recordset()
        rows = self._review_rows(ev)
        row = next(x for x in rows if x["type"] == "subjective_rubric")
        self.assertEqual(row["verdict"], "Awaiting grading")
