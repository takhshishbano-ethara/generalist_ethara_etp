import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.etp_assessment_pro.services import scoring as scoring_svc
from odoo.addons.etp_assessment_pro.services import vertex as vertex_svc


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.ResponseLine = self.env["etp.assessment.pro.response.line"]
        self.Applicant = self.env["hr.applicant"]
        self.Users = self.env["res.users"]
        self.Category = self.env["etp.assessment.pro.prompt"]
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]

    def _make_applicant(self, name):
        slug = name.lower().replace(" ", "_")
        email = f"{slug}_{uuid4().hex[:8]}@x.com"
        portal = self.env.ref("base.group_portal")
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": name, "login": email, "email": email,
            "group_ids": [(6, 0, [portal.id])],
        })
        return self.env["hr.applicant"].create({
            "partner_name": name,
            "email_from": email,
            "partner_id": user.partner_id.id,
            "candidate_user_id": user.id,
        })

    def _make_skill(self, name="Skill1", qcount=2, mins=10, qtype="mcq"):
        return self.Category.create({"name": name})

    def _make_category(self, name="Cat1"):
        return self.Category.create({"name": name})

    def _make_mcq(self, name="Q-MCQ", correct_idx=0, skill=None, category=None,
                  opt_names=("A", "B", "C")):
        q_vals = {
            "name": name,
            "question_type": "mcq",
            "prompt": "Pick the right one",
            "difficulty": "easy",
        }
        if skill:
            q_vals["generator_id"] = skill.id
        elif category:
            q_vals["generator_id"] = category.id
        q = self.Question.create(q_vals)
        qd = self.QDim.create({
            "question_id": q.id,
            "name": f"Dim_{name}",
            "option_line_ids": [
                (0, 0, {"name": n, "sequence": idx * 10,
                        "is_correct": idx == correct_idx})
                for idx, n in enumerate(opt_names)
            ],
        })
        opts = qd.option_line_ids.sorted("sequence")
        return q, qd, opts

    def _make_msq(self, name="Q-MSQ", correct_idxs=(0, 1), skill=None,
                  category=None, opt_names=("A", "B", "C", "D")):
        q_vals = {
            "name": name,
            "question_type": "msq",
            "prompt": "Pick all correct ones",
            "difficulty": "medium",
        }
        if skill:
            q_vals["generator_id"] = skill.id
        elif category:
            q_vals["generator_id"] = category.id
        q = self.Question.create(q_vals)
        qd = self.QDim.create({
            "question_id": q.id,
            "name": f"Dim_{name}",
            "option_line_ids": [
                (0, 0, {"name": n, "sequence": idx * 10,
                        "is_correct": idx in correct_idxs})
                for idx, n in enumerate(opt_names)
            ],
        })
        opts = qd.option_line_ids.sorted("sequence")
        return q, qd, opts

    def _make_subjective(self, name="Q-SUBJ", skill=None, category=None,
                         qtype="subjective_rubric"):
        vals = {
            "name": name,
            "question_type": qtype,
            "prompt": "Explain your reasoning",
            "difficulty": "medium",
        }
        if skill:
            vals["generator_id"] = skill.id
        elif category:
            vals["generator_id"] = category.id
        return self.Question.create(vals)

    def _make_single_assessment(self, category, num_candidates=1, qlimit=0):
        emps = [self._make_applicant(f"Emp_{i}") for i in range(num_candidates)]
        a = self.Assessment.create({
            "name": "T1",
            "generator_id": category.id,
            "question_limit": qlimit,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [e.id for e in emps])],
        })
        return a, emps


class TestAssessmentLifecycle(_Base):

    def test_create_single_mode_defaults(self):
        cat = self._make_category()
        a = self.Assessment.create({
            "name": "S",
            "generator_id": cat.id,
        })
        self.assertEqual(a.state, "draft")
        self.assertEqual(a.generator_id, cat)

    def test_single_mode_start(self):
        cat = self._make_category()
        skill = self._make_skill()
        self._make_mcq("Q1", correct_idx=0, category=cat)
        self._make_mcq("Q2", correct_idx=1, category=cat)
        a, emps = self._make_single_assessment(cat, num_candidates=1, qlimit=2)
        a.action_start()
        self.assertEqual(a.state, "in_progress")
        self.assertEqual(len(a.assessment_evaluator_ids), 1)
        ev = a.assessment_evaluator_ids
        order = json.loads(ev.question_order or "[]")
        self.assertEqual(len(order), 2)
        self.assertEqual(ev.total_questions, 2)

    def test_single_mode_requires_generator(self):
        emp = self._make_applicant("NoGen")
        a = self.Assessment.create({
            "name": "Bad",
            "generator_id": False,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        with self.assertRaises(UserError):
            a.action_start()


class TestScoring(_Base):

    def _build_response(self, question, evaluator, picks=(), justification=""):
        line_vals = []
        if question.question_type in ("mcq", "msq"):
            qd = question.question_dimension_ids[0]
            for opt_id in picks:
                line_vals.append((0, 0, {
                    "question_dimension_id": qd.id,
                    "selected_option_id": opt_id,
                }))
        return self.Response.create({
            "assessment_id": evaluator.assessment_id.id,
            "assessment_evaluator_id": evaluator.id,
            "evaluator_id": evaluator.applicant_id.id,
            "question_id": question.id,
            "justification": justification,
            "line_ids": line_vals,
        })

    def _basic_assessment(self, question):
        cat = question.generator_id
        if not cat:
            cat = self._make_category()
            question.generator_id = cat.id
        emp = self._make_applicant("Solo")
        a = self.Assessment.create({
            "name": "Sc",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.action_start()
        return a, a.assessment_evaluator_ids[0]

    def test_mcq_correct_full_score(self):
        q, dim, master = self._make_mcq("MCQ1", correct_idx=0)
        _, ev = self._basic_assessment(q)
        r = self._build_response(q, ev, picks=[master[0].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, r.max_score)
        self.assertEqual(r.max_score, 1)

    def test_mcq_wrong_zero(self):
        q, dim, master = self._make_mcq("MCQw", correct_idx=0)
        _, ev = self._basic_assessment(q)
        r = self._build_response(q, ev, picks=[master[1].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, 0)
        self.assertEqual(r.max_score, 1)

    def test_mcq_partial_zero(self):
        q, dim, master = self._make_mcq("MCQp", correct_idx=2)
        _, ev = self._basic_assessment(q)
        r = self._build_response(q, ev, picks=[master[0].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, 0)

    def test_msq_exact_match_full(self):
        q, dim, master = self._make_msq("MSQ_exact", correct_idxs=(0, 1))
        _, ev = self._basic_assessment(q)
        r = self._build_response(q, ev, picks=[master[0].id, master[1].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, r.max_score)
        self.assertTrue(r.max_score > 0)

    def test_msq_missing_one_zero(self):
        q, dim, master = self._make_msq("MSQ_miss", correct_idxs=(0, 1))
        _, ev = self._basic_assessment(q)
        r = self._build_response(q, ev, picks=[master[0].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, 0)

    def test_msq_extra_zero(self):
        q, dim, master = self._make_msq("MSQ_extra", correct_idxs=(0, 1))
        _, ev = self._basic_assessment(q)
        r = self._build_response(
            q, ev, picks=[master[0].id, master[1].id, master[2].id])
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.score, 0)

    def test_subjective_rubric_needs_llm(self):
        cat = self._make_category()
        q = self._make_subjective("S1", category=cat)
        emp = self._make_applicant("Cand")
        a = self.Assessment.create({
            "name": "Sub",
            "generator_id": cat.id,
            "question_limit": 1, "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q.id])]})
        ev = self.Evaluator.create({
            "assessment_id": a.id,
            "applicant_id": emp.id,
            "question_order": json.dumps([q.id]),
            "total_questions": 1,
        })
        r = self._build_response(q, ev, justification="My answer is X")
        self.assertTrue(r.needs_llm)
        self.assertFalse(r.has_objective)

    def _subj_setup(self, justification="My reasoned answer."):
        cat = self._make_category()
        q = self._make_subjective("S2", category=cat)
        emp = self._make_applicant("Subj")
        a = self.Assessment.create({
            "name": "AS",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        ev = self.Evaluator.create({
            "assessment_id": a.id,
            "applicant_id": emp.id,
            "question_order": json.dumps([q.id]),
            "total_questions": 1,
            "state": "submitted",
        })
        r = self._build_response(q, ev, justification=justification)
        r.write({"state": "submitted", "llm_state": "pending"})
        return a, ev, q, r

    def test_subjective_score_above_threshold_passes(self):
        a, ev, q, r = self._subj_setup()
        fake = json.dumps([{"id": r.id, "score": 0.9, "feedback": "good"}])
        with patch.object(vertex_svc, "_call_vertex", return_value=fake):
            scored = scoring_svc.score_evaluator(self.env, ev)
        self.assertEqual(scored, 1)
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "scored")
        self.assertTrue(r.llm_passed)
        # EQUAL MARKS: every question worth 1; pass earns the single mark.
        self.assertEqual(r.llm_score, 1)
        self.assertEqual(r.llm_max_score, 1)

    def test_subjective_score_below_threshold_fails(self):
        a, ev, q, r = self._subj_setup()
        fake = json.dumps([{"id": r.id, "score": 0.5, "feedback": "weak"}])
        with patch.object(vertex_svc, "_call_vertex", return_value=fake):
            scored = scoring_svc.score_evaluator(self.env, ev)
        self.assertEqual(scored, 1)
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "scored")
        self.assertFalse(r.llm_passed)
        self.assertEqual(r.llm_score, 0)
        self.assertEqual(r.llm_max_score, 1)

    def test_evaluator_score_percent(self):
        cat = self._make_category()
        q_mcq, dim, master = self._make_mcq("EVQ1", correct_idx=0, category=cat)
        q_subj = self._make_subjective("EVQ2", category=cat)
        emp = self._make_applicant("Both")
        a = self.Assessment.create({
            "name": "B",
            "generator_id": cat.id, "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        r_mcq = self._build_response(q_mcq, ev, picks=[master[0].id])
        r_mcq.action_submit()
        r_subj = self._build_response(q_subj, ev, justification="reasoned answer")
        r_subj.write({"state": "submitted", "llm_state": "pending"})

        fake = json.dumps([{"id": r_subj.id, "score": 0.9, "feedback": "y"}])
        ev.write({"state": "submitted"})
        with patch.object(vertex_svc, "_call_vertex", return_value=fake):
            scoring_svc.score_evaluator(self.env, ev)
        ev.invalidate_recordset()
        # EQUAL MARKS: mcq mark 1, subjective mark 1, denominator = 2 questions.
        self.assertEqual(ev.total_score, 1)
        self.assertEqual(ev.llm_total_score, 1)
        self.assertAlmostEqual(ev.score_percent, 100.0, places=1)


class TestEnqueueScoring(_Base):

    def _ctx(self, llm_auto=False, justification="Answer text"):
        cat = self._make_category()
        q = self._make_subjective("EQ", category=cat)
        emp = self._make_applicant("EnqCand")
        a = self.Assessment.create({
            "name": "EnqA",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30, "llm_auto_score": llm_auto,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q.id])]})
        ev = self.Evaluator.create({
            "assessment_id": a.id,
            "applicant_id": emp.id,
            "question_order": json.dumps([q.id]),
            "total_questions": 1,
        })
        r = self.Response.create({
            "assessment_id": a.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id,
            "question_id": q.id,
            "justification": justification,
        })
        return a, ev, q, r

    def test_response_action_submit_enqueues_subjective(self):
        a, ev, q, r = self._ctx(llm_auto=False)
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "pending")

    def test_response_action_submit_skips_no_justification_subjective(self):
        cat = self._make_category()
        q, dim, master = self._make_mcq("NoJustMCQ", category=cat)
        emp = self._make_applicant("NJ")
        a = self.Assessment.create({
            "name": "NJA",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        r = self.Response.create({
            "assessment_id": a.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id,
            "question_id": q.id,
            "justification": "",
            "line_ids": [(0, 0, {
                "question_dimension_id": dim.id,
                "selected_option_id": master[0].id,
            })],
        })
        r.action_submit()
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "not_needed")

    def test_cron_llm_auto_score_drains_pending(self):
        """llm_auto_score ON: submitting auto-queues, the cron drains it.

        Submission itself sets scoring_requested (see Evaluator.write) so no
        admin has to click 'Run Subjective Evaluation'. The Vertex call still
        happens in the cron, never in the submit request.
        """
        a, ev, q, r = self._ctx(llm_auto=True)
        r.write({"state": "submitted", "llm_state": "pending"})
        ev.write({"state": "submitted"})
        ev.invalidate_recordset()
        self.assertTrue(
            ev.scoring_requested,
            "submitting with llm_auto_score ON must auto-queue grading")
        fake = json.dumps([{"id": r.id, "score": 0.95, "feedback": "ok"}])
        with patch.object(vertex_svc, "_call_vertex", return_value=fake), \
                patch.object(self.env.cr, "commit"):
            self.Assessment._cron_llm_auto_score()
        r.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertEqual(r.llm_state, "scored")
        self.assertEqual(r.llm_score, 1)
        self.assertEqual(ev.llm_state, "scored")

    def test_submit_does_not_queue_when_llm_auto_score_off(self):
        """llm_auto_score OFF is the kill switch on Vertex spend: submitting
        must NOT queue, and the cron must leave the answer pending until an
        admin explicitly requests it."""
        a, ev, q, r = self._ctx(llm_auto=False)
        r.write({"state": "submitted", "llm_state": "pending"})
        ev.write({"state": "submitted"})
        ev.invalidate_recordset()
        self.assertFalse(
            ev.scoring_requested,
            "llm_auto_score OFF must not auto-queue grading")
        fake = json.dumps([{"id": r.id, "score": 0.95, "feedback": "ok"}])
        with patch.object(vertex_svc, "_call_vertex", return_value=fake), \
                patch.object(self.env.cr, "commit"):
            self.Assessment._cron_llm_auto_score()
        r.invalidate_recordset()
        self.assertEqual(r.llm_state, "pending")


class TestRecordRules(_Base):

    def _user_with_applicant(self, name, login, group_xmlid):
        grp = self.env.ref(group_xmlid)
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": name, "login": login, "email": f"{login}@x.com",
            "group_ids": [(4, grp.id)],
        })
        emp = self.Applicant.create({
            "partner_name": name,
            "email_from": f"{login}@x.com",
            "partner_id": user.partner_id.id,
            "candidate_user_id": user.id,
        })
        return user, emp

    def _full_setup(self):
        u1, e1 = self._user_with_applicant(
            "Cand1", "rcand1", "etp_assessment_pro.group_assessment_evaluator")
        u2, e2 = self._user_with_applicant(
            "Cand2", "rcand2", "etp_assessment_pro.group_assessment_evaluator")
        mgr_user, _ = self._user_with_applicant(
            "Mgr", "rmgr", "etp_assessment_pro.group_assessment_manager")

        cat = self._make_category()
        q, dim, master = self._make_mcq("RR_Q", category=cat)

        a1 = self.Assessment.create({
            "name": "RR_A1",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [e1.id])],
        })
        a1.action_start()
        a2 = self.Assessment.create({
            "name": "RR_A2",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [e2.id])],
        })
        a2.action_start()
        ev1 = a1.assessment_evaluator_ids
        ev2 = a2.assessment_evaluator_ids

        r1 = self.Response.create({
            "assessment_id": a1.id,
            "assessment_evaluator_id": ev1.id,
            "evaluator_id": e1.id,
            "question_id": q.id,
            "line_ids": [(0, 0, {
                "question_dimension_id": dim.id,
                "selected_option_id": master[0].id,
            })],
        })
        r2 = self.Response.create({
            "assessment_id": a2.id,
            "assessment_evaluator_id": ev2.id,
            "evaluator_id": e2.id,
            "question_id": q.id,
            "line_ids": [(0, 0, {
                "question_dimension_id": dim.id,
                "selected_option_id": master[0].id,
            })],
        })
        return {
            "u1": u1, "u2": u2, "mgr": mgr_user,
            "e1": e1, "e2": e2,
            "ev1": ev1, "ev2": ev2,
            "r1": r1, "r2": r2,
        }

    def test_evaluator_sees_only_own_evaluator(self):
        s = self._full_setup()
        u1_evs = self.Evaluator.with_user(s["u1"]).search([])
        self.assertIn(s["ev1"], u1_evs)
        self.assertNotIn(s["ev2"], u1_evs)

    def test_evaluator_sees_only_own_response(self):
        s = self._full_setup()
        u1_resps = self.Response.with_user(s["u1"]).search([])
        self.assertIn(s["r1"], u1_resps)
        self.assertNotIn(s["r2"], u1_resps)

    def test_manager_sees_all(self):
        s = self._full_setup()
        mgr_evs = self.Evaluator.with_user(s["mgr"]).search([])
        self.assertIn(s["ev1"], mgr_evs)
        self.assertIn(s["ev2"], mgr_evs)
        mgr_resps = self.Response.with_user(s["mgr"]).search([])
        self.assertIn(s["r1"], mgr_resps)
        self.assertIn(s["r2"], mgr_resps)


class TestExportResults(_Base):

    def _setup(self):
        cat = self._make_category()
        q, dim, master = self._make_mcq("XQ", category=cat)
        emp = self._make_applicant("XCand")
        a = self.Assessment.create({
            "name": "ExA",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        r = self.Response.create({
            "assessment_id": a.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id,
            "question_id": q.id,
            "line_ids": [(0, 0, {
                "question_dimension_id": dim.id,
                "selected_option_id": master[0].id,
            })],
        })
        r.action_submit()
        return a

    def test_action_export_results_returns_url_action(self):
        a = self._setup()
        action = a.action_export_results()
        self.assertEqual(action.get("type"), "ir.actions.act_url")
        self.assertIn("/web/content/", action.get("url", ""))

    def test_export_payload_has_expected_columns(self):
        import base64
        a = self._setup()
        action = a.action_export_results()
        url = action["url"]
        att_id = int(url.split("/web/content/")[1].split("?")[0])
        att = self.env["ir.attachment"].browse(att_id)
        self.assertTrue(att.exists())
        content = base64.b64decode(att.datas).decode("utf-8")
        header = content.splitlines()[0]
        for col in ("rank", "candidate", "email", "score_percent", "result"):
            self.assertIn(col, header)


class TestResultSummary(_Base):
    """The per-candidate Result Summary card must reflect the SAME scored
    values shown in the detailed breakdown - objective correct/total,
    subjective pass/total, totals, percent, and pass/fail badge."""

    def _submitted_mixed(self, mcq_correct=True, subj_score=0.9):
        cat = self._make_category()
        q_mcq, dim, master = self._make_mcq("RSQ1", correct_idx=0, category=cat)
        q_subj = self._make_subjective("RSQ2", category=cat)
        emp = self._make_applicant("RSCand")
        a = self.Assessment.create({
            "name": "RS",
            "generator_id": cat.id, "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        pick = master[0].id if mcq_correct else master[1].id
        r_mcq = self.Response.create({
            "assessment_id": a.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id, "question_id": q_mcq.id,
            "line_ids": [(0, 0, {"question_dimension_id": dim.id,
                                 "selected_option_id": pick})],
        })
        r_mcq.action_submit()
        r_subj = self.Response.create({
            "assessment_id": a.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id, "question_id": q_subj.id,
            "justification": "A reasoned answer about the topic.",
        })
        r_subj.write({"state": "submitted", "llm_state": "pending"})
        ev.write({"state": "submitted"})
        fake = json.dumps([{"id": r_subj.id, "score": subj_score,
                            "feedback": "fb"}])
        with patch.object(vertex_svc, "_call_vertex", return_value=fake):
            scoring_svc.score_evaluator(self.env, ev)
        ev.invalidate_recordset()
        return ev

    def test_summary_renders_for_submitted(self):
        ev = self._submitted_mixed(mcq_correct=True, subj_score=0.9)
        html = ev.result_summary or ""
        self.assertTrue(html)
        self.assertIn("PASS", html)
        # objective 1/1 correct, subjective 1/1 passed
        self.assertIn("1 / 1 correct", html)
        self.assertIn("1 / 1 passed", html)

    def test_summary_fail_reflects_scores(self):
        # MCQ wrong + subjective below threshold -> 0% -> FAIL
        ev = self._submitted_mixed(mcq_correct=False, subj_score=0.3)
        html = ev.result_summary or ""
        self.assertIn("FAIL", html)
        self.assertIn("0 / 1 correct", html)
        self.assertIn("0 / 1 passed", html)

    def test_summary_pending_before_submit(self):
        cat = self._make_category()
        self._make_mcq("PreQ", correct_idx=0, category=cat)
        a, emps = self._make_single_assessment(cat, num_candidates=1, qlimit=1)
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        # Not submitted yet -> neutral in-progress note, no PASS/FAIL.
        html = ev.result_summary or ""
        self.assertIn("has not submitted", html)
        self.assertNotIn(">PASS<", html)

    def test_summary_totals_match_evaluator_fields(self):
        ev = self._submitted_mixed(mcq_correct=True, subj_score=0.9)
        # The card's totals must equal the stored scoring fields exactly.
        # EQUAL MARKS: a passed subjective answer is worth 1, not `points`.
        total = (ev.total_score or 0) + (ev.llm_total_score or 0)
        self.assertEqual(ev.llm_total_score, 1)
        self.assertIn("%s / %s pts" % (total, ev.max_possible_score
                                       + ev.llm_max_score),
                      ev.result_summary or "")

    def test_export_responses_has_summary_row(self):
        import base64
        import csv as _csv
        import io as _io
        from odoo.addons.etp_assessment_pro.services import export as export_svc
        ev = self._submitted_mixed(mcq_correct=True, subj_score=0.9)
        action = export_svc.export_responses(ev.assessment_id, evaluator=ev)
        att = self.env["ir.attachment"].browse(
            int(action["url"].split("/web/content/")[1].split("?")[0]))
        text = base64.b64decode(att.datas).decode("utf-8")
        rows = list(_csv.DictReader(_io.StringIO(text)))
        summary = [r for r in rows if r["question"] == "[RESULT SUMMARY]"]
        self.assertEqual(len(summary), 1,
                         "exactly one summary row per candidate")
        s = summary[0]
        # The summary row must be the FIRST row (top of the candidate block).
        self.assertEqual(rows[0]["question"], "[RESULT SUMMARY]")
        # Its totals must equal the evaluator's stored scoring fields.
        self.assertEqual(int(s["objective_score"]), ev.total_score)
        self.assertEqual(int(s["subjective_mark"]), ev.llm_total_score)
        self.assertEqual(int(s["total_score"]),
                         ev.total_score + ev.llm_total_score)
        self.assertEqual(s["subjective_result"], ev.result)
        self.assertIn("correct", s["candidate_answer"])


class TestCsvImportAnswerKeyIntegrity(_Base):
    """The CSV importer must materialize each question's answer key EXACTLY
    as declared - never lose a correct flag, never flag a wrong option, and
    never let two questions sharing a dimension label contaminate each other.
    """

    def _import_csv(self, csv_text, name="bank.csv"):
        import base64
        Wizard = self.env["etp.assessment.pro.bank.import.wizard"]
        wiz = Wizard.create({
            "data_file": base64.b64encode(csv_text.encode("utf-8")).decode(),
            "data_filename": name,
            "generator_name": "T %s" % name,
        })
        res = wiz.action_import()
        prompt = self.env["etp.assessment.pro.prompt"].browse(res["res_id"])
        return prompt.question_ids.sorted("id").mapped("approved_question_id")

    def _correct_names(self, question):
        out = {}
        for qd in question.question_dimension_ids:
            out[qd.name] = sorted(
                ol.name for ol in qd.option_line_ids.filtered("is_correct"))
        return out

    def test_single_dim_mcq_correct_flag(self):
        csv_text = (
            "title,question_type,prompt,options,correct_answer\n"
            "Cap,mcq,Capital of France?,Paris|London|Berlin,Paris\n")
        qs = self._import_csv(csv_text)
        self.assertEqual(len(qs), 1)
        keys = self._correct_names(qs[0])
        # exactly one correct option, and it is Paris
        all_correct = [c for v in keys.values() for c in v]
        self.assertEqual(all_correct, ["Paris"])

    def test_shared_label_questions_do_not_contaminate(self):
        # Two image_ab rows share identical dimension labels but DIFFER on the
        # 3rd axis's correct answer - the real adversarial case from the
        # reference bank. Each must keep its OWN key.
        dims_a = json.dumps([
            {"label": "Edit correct?", "options": ["Aligned", "No"],
             "correct": ["Aligned"]},
            {"label": "Slop free?", "options": ["Clean", "No"],
             "correct": ["Clean"]},
        ])
        dims_b = json.dumps([
            {"label": "Edit correct?", "options": ["Aligned", "No"],
             "correct": ["Aligned"]},
            {"label": "Slop free?", "options": ["Clean", "No"],
             "correct": ["No"]},   # <-- opposite answer, same label
        ])
        csv_text = (
            "title,question_type,prompt,dimensions_json\n"
            'QA,image_ab,Compare,"%s"\n'
            'QB,image_ab,Compare,"%s"\n'
            % (dims_a.replace('"', '""'), dims_b.replace('"', '""')))
        qs = self._import_csv(csv_text, name="shared.csv")
        self.assertEqual(len(qs), 2)
        ka = self._correct_names(qs[0])
        kb = self._correct_names(qs[1])
        self.assertEqual(ka["Slop free?"], ["Clean"])
        self.assertEqual(kb["Slop free?"], ["No"])
        # And neither question's dimension is shared (private per question).
        a_dims = set(qs[0].question_dimension_ids.ids)
        b_dims = set(qs[1].question_dimension_ids.ids)
        self.assertFalse(a_dims & b_dims,
                         "questions must not share dimension records")

    def test_index_correct_answer_resolves(self):
        # correct given as a 0-based index string must map to the right option.
        csv_text = (
            "title,question_type,prompt,options,correct_answer\n"
            "Idx,mcq,Pick,Alpha|Beta|Gamma,1\n")  # index 1 -> Beta
        qs = self._import_csv(csv_text, name="idx.csv")
        all_correct = [c for v in self._correct_names(qs[0]).values()
                       for c in v]
        self.assertEqual(all_correct, ["Beta"])


class TestManagerApplicantAccess(_Base):
    """BUG-009 regression: an assessment Manager who is NOT separately granted
    a recruitment role must still be able to read hr.applicant - every
    assessment/evaluator view dereferences applicant_id, so without this the
    admin hits 'You are not allowed to access Applicant (hr.applicant)'.
    The manager group implies hr_recruitment.group_hr_recruitment_user to
    grant that access through the proper ACL.
    """

    def _manager_user(self):
        from uuid import uuid4
        mgr = self.env.ref("etp_assessment_pro.group_assessment_manager")
        email = "mgracl_%s@ethara.ai" % uuid4().hex[:6]
        return self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Mgr ACL", "login": email, "email": email,
                "group_ids": [(6, 0, [mgr.id])]})

    def test_manager_can_read_applicant(self):
        user = self._manager_user()
        # Must not raise AccessError.
        self.env["hr.applicant"].with_user(user).search_count([])

    def test_manager_can_open_candidate_via_evaluator(self):
        cat = self._make_category()
        self._make_mcq("AclQ", correct_idx=0, category=cat)
        a, emps = self._make_single_assessment(cat, num_candidates=1, qlimit=1)
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        user = self._manager_user()
        # The view path: read the evaluator and dereference the candidate name.
        name = ev.with_user(user).applicant_id.partner_name
        self.assertTrue(name is not None)
