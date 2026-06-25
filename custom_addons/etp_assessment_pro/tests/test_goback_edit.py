import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.controllers import portal as portal_ctrl


class _FakeRequest:
    """Minimal request stand-in: _record_response only touches request.env."""
    def __init__(self, env):
        self.env = env


class TestGoBackEditPersists(TransactionCase):
    """Repro for the live-test data-integrity bugs (2026-06-25):

    1. Editing a justification and re-saving (going back) is silently dropped.
    2. Changing an MCQ option on a second pass is silently dropped.
    3. A justification added on a second pass never flips needs_llm.

    Root cause hypothesis: _record_response early-returns when the existing
    response is already in state 'submitted' (every save submits), so any later
    edit from free back-navigation is discarded.
    """

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.dim_if = self.env.ref("etp_assessment_pro.dim_image_if")

    def _evaluator(self):
        applicant = self.Applicant.create({
            "partner_name": "Back Cand", "email_from": "back@example.com"})
        assessment = self.Assessment.create({"name": "Back Assessment"})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
        })
        return ev

    def _make_mcq(self):
        """Build an mcq question with one objective dimension, 2 options."""
        master = self.env["etp.assessment.pro.dimension"].create({
            "name": "Pick",
        })
        opt_a = self.env["etp.assessment.pro.dimension.option"].create({
            "dimension_id": master.id, "name": "Alpha"})
        opt_b = self.env["etp.assessment.pro.dimension.option"].create({
            "dimension_id": master.id, "name": "Beta"})
        q = self.Question.create({
            "name": "MCQ", "prompt": "Pick one.", "question_type": "mcq"})
        qd = self.QDim.create({
            "question_id": q.id, "dimension_id": master.id})
        for line in qd.option_line_ids:
            line.is_correct = line.master_option_id.id == opt_a.id
        return q, master, opt_a, opt_b

    def _make_subjective(self):
        return self.Question.create({
            "name": "Subj", "prompt": "Explain.",
            "question_type": "subjective_justification"})

    def test_justification_edit_on_goback_persists(self):
        q = self._make_subjective()
        ev = self._evaluator()
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            # First pass: candidate writes an answer.
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "justification": "First draft answer."})
            # Candidate navigates back and edits the answer.
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "justification": "EDITED final answer."})
        resp = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q.id)])
        self.assertEqual(len(resp), 1, "should not create duplicate responses")
        self.assertEqual(
            resp.justification, "EDITED final answer.",
            "edited justification must persist when going back")

    def test_mcq_option_change_on_goback_persists(self):
        q, master, opt_a, opt_b = self._make_mcq()
        ev = self._evaluator()
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            # First pass: pick Beta (wrong).
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "dimension_%d" % master.id: str(opt_b.id)})
            # Go back, change to Alpha (correct).
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "dimension_%d" % master.id: str(opt_a.id)})
        resp = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q.id)])
        chosen = resp.line_ids.mapped("selected_option_id.id")
        self.assertEqual(
            chosen, [opt_a.id],
            "changed MCQ option must persist when going back")

    def _make_msq(self):
        """Build an msq question with one dimension, 3 options (A,B correct)."""
        master = self.env["etp.assessment.pro.dimension"].create({
            "name": "MultiPick",
        })
        opts = [
            self.env["etp.assessment.pro.dimension.option"].create({
                "dimension_id": master.id, "name": n})
            for n in ("One", "Two", "Three")]
        q = self.Question.create({
            "name": "MSQ", "prompt": "Pick all.", "question_type": "msq"})
        qd = self.QDim.create({
            "question_id": q.id, "dimension_id": master.id})
        for line in qd.option_line_ids:
            line.is_correct = line.master_option_id.id in (
                opts[0].id, opts[1].id)
        return q, master, opts

    def test_added_justification_on_goback_sets_needs_llm(self):
        q = self._make_subjective()
        ev = self._evaluator()
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            # First save with a (placeholder-ish) short answer, then a real one.
            ctrl._record_response(ev, None, {
                "question_id": str(q.id), "justification": "x"})
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "justification": "A thorough, real justification."})
        resp = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q.id)])
        resp.invalidate_recordset()
        self.assertEqual(resp.justification, "A thorough, real justification.")
        self.assertTrue(
            resp.needs_llm,
            "a real justification must mark the response as needs_llm")

    def test_msq_picks_change_on_goback_persists(self):
        q, master, opts = self._make_msq()
        ev = self._evaluator()
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            # First pass: pick One+Three (CSV as the runner serializes it).
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "dimension_%d" % master.id: "%d,%d" % (opts[0].id, opts[2].id)})
            # Go back, change selection to One+Two (the correct set).
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "dimension_%d" % master.id: "%d,%d" % (opts[0].id, opts[1].id)})
        resp = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q.id)])
        chosen = sorted(resp.line_ids.mapped("selected_option_id.id"))
        self.assertEqual(
            chosen, sorted([opts[0].id, opts[1].id]),
            "changed MSQ multi-select must persist when going back")


class TestScoreConsistency(TransactionCase):
    """Repro for 'score shown after submission is incorrect and different for
    everyone'. Two candidates take the SAME 2-MCQ assessment; the denominator
    (max score) MUST be identical for both regardless of how many they got
    right, and the percentage must reflect the all-or-nothing per-question
    objective scoring.
    """

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.Category = self.env["etp.assessment.pro.category"]

    def _mcq(self, name, category):
        master = self.env["etp.assessment.pro.dimension"].create({
            "name": "Dim_%s" % name})
        opts = [
            self.env["etp.assessment.pro.dimension.option"].create({
                "dimension_id": master.id, "name": n}) for n in ("A", "B")]
        q = self.Question.create({
            "name": name, "prompt": "Pick one.", "question_type": "mcq",
            "category_id": category.id})
        qd = self.QDim.create({"question_id": q.id, "dimension_id": master.id})
        for line in qd.option_line_ids:
            line.is_correct = line.master_option_id.id == opts[0].id  # A correct
        return q, master, opts

    def _answer(self, ev, q, master, opt):
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            ctrl._record_response(ev, None, {
                "question_id": str(q.id),
                "dimension_%d" % master.id: str(opt.id)})

    def test_denominator_identical_across_candidates(self):
        cat = self.Category.create({"name": "ScoreCat"})
        q1, m1, o1 = self._mcq("SC1", cat)
        q2, m2, o2 = self._mcq("SC2", cat)
        emps = [self.Applicant.create({
            "partner_name": "C%d" % i, "email_from": "c%d@x.com" % i})
            for i in range(2)]
        a = self.Assessment.create({
            "name": "ScoreA", "assessment_mode": "single",
            "category_id": cat.id, "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [e.id for e in emps])]})
        a.write({"question_ids": [(6, 0, [q1.id, q2.id])]})
        a.action_start()
        ev0, ev1 = a.assessment_evaluator_ids[0], a.assessment_evaluator_ids[1]
        # Candidate 0: both correct (A, A).
        self._answer(ev0, q1, m1, o1[0])
        self._answer(ev0, q2, m2, o2[0])
        # Candidate 1: one correct, one wrong (A, B).
        self._answer(ev1, q1, m1, o1[0])
        self._answer(ev1, q2, m2, o2[1])
        for ev in (ev0, ev1):
            ev.invalidate_recordset()
        # Denominator MUST match — both took the same 2 single-point questions.
        self.assertEqual(
            ev0.max_possible_score, ev1.max_possible_score,
            "max score (denominator) must be identical for the same test")
        self.assertEqual(ev0.max_possible_score, 2)
        # Candidate 0 perfect, candidate 1 half.
        self.assertEqual(ev0.total_score, 2)
        self.assertEqual(ev1.total_score, 1)
        self.assertEqual(ev0.score_percent, 100.0)
        self.assertEqual(ev1.score_percent, 50.0)

    def test_denominator_includes_unanswered_after_finish(self):
        """A candidate who answers only SOME questions then finishes must still
        be scored against ALL assigned questions — the finish path fills
        placeholders so the denominator is the full test, not just what they
        answered (else answering 1/2 perfectly reads as 100%).
        """
        cat = self.Category.create({"name": "ScoreCat2"})
        q1, m1, o1 = self._mcq("UN1", cat)
        q2, m2, o2 = self._mcq("UN2", cat)
        emp = self.Applicant.create({
            "partner_name": "Partial", "email_from": "partial@x.com"})
        a = self.Assessment.create({
            "name": "PartialA", "assessment_mode": "single",
            "category_id": cat.id, "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])]})
        a.write({"question_ids": [(6, 0, [q1.id, q2.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        # Answer only q1 (correctly), leave q2 unanswered, then finish.
        self._answer(ev, q1, m1, o1[0])
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            ctrl._auto_submit_remaining_single(ev)
        ev.invalidate_recordset()
        # Denominator must be 2 (both questions), not 1.
        self.assertEqual(
            ev.max_possible_score, 2,
            "unanswered questions must count toward the denominator")
        self.assertEqual(ev.total_score, 1)
        self.assertEqual(ev.score_percent, 50.0)


class TestMandatoryAndDuration(TransactionCase):
    """#7 final submit blocked until required questions answered, and
    #8 day-plan generation blocked when a day has no duration (no timer)."""

    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Applicant = self.env["hr.applicant"]
        self.Category = self.env["etp.assessment.pro.category"]
        self.Skill = self.env["etp.assessment.pro.skill"]

    def _mcq(self, name, category):
        master = self.env["etp.assessment.pro.dimension"].create({
            "name": "Dim_%s" % name})
        opts = [self.env["etp.assessment.pro.dimension.option"].create({
            "dimension_id": master.id, "name": n}) for n in ("A", "B")]
        q = self.Question.create({
            "name": name, "prompt": "Pick.", "question_type": "mcq",
            "category_id": category.id})
        qd = self.QDim.create({"question_id": q.id, "dimension_id": master.id})
        for line in qd.option_line_ids:
            line.is_correct = line.master_option_id.id == opts[0].id
        return q, master, opts

    def test_unanswered_blocks_final_submit(self):
        cat = self.Category.create({"name": "MandCat"})
        q1, m1, o1 = self._mcq("MD1", cat)
        q2, m2, o2 = self._mcq("MD2", cat)
        emp = self.Applicant.create({
            "partner_name": "Mand", "email_from": "m@x.com"})
        a = self.Assessment.create({
            "name": "MandA", "assessment_mode": "single",
            "category_id": cat.id, "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])]})
        a.write({"question_ids": [(6, 0, [q1.id, q2.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            # Answer only q1.
            ctrl._record_response(ev, None, {
                "question_id": str(q1.id),
                "dimension_%d" % m1.id: str(o1[0].id)})
            unanswered = ctrl._unanswered_question_ids(ev, False)
        self.assertEqual(
            unanswered, [q2.id],
            "q2 must be reported as unanswered, blocking final submit")

    def test_generate_plan_blocks_zero_duration_day(self):
        skill = self.Skill.create({
            "name": "DurSkill", "question_type": "mcq", "question_count": 1,
            "time_minutes": 0})
        cat = self.Category.create({"name": "DurCat"})
        self._mcq("DurQ", cat)
        emp = self.Applicant.create({
            "partner_name": "Dur", "email_from": "d@x.com"})
        a = self.Assessment.create({
            "name": "DurA", "assessment_mode": "multi_day", "num_days": 1,
            "sequential_days": True,
            "evaluator_ids": [(6, 0, [emp.id])]})
        a.action_scaffold_days()
        # Day with a category pool but duration left at 0 (the forgotten field).
        a.day_ids[0].write({
            "pool_by": "category", "category_id": cat.id,
            "question_count": 1, "duration_minutes": 0})
        from odoo.exceptions import UserError
        with self.assertRaises(UserError) as cm:
            a.action_generate_plan()
        self.assertIn("no time limit", str(cm.exception).lower())




