import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.etp_assessment_pro.services import vertex, scoring, consistency
from odoo.addons.etp_assessment_pro.controllers import portal as portal_ctrl


class _FakeRequest:
    def __init__(self, env):
        self.env = env


class _Base(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Question = self.env["etp.assessment.pro.question"]
        self.QDim = self.env["etp.assessment.pro.question.dimension"]
        self.Response = self.env["etp.assessment.pro.response"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.dim_if = self.env.ref("etp_assessment_pro.dim_image_if")
        self.dim_vq = self.env.ref("etp_assessment_pro.dim_image_vq")
        self.dim_lai = self.env.ref("etp_assessment_pro.dim_image_lai")
        self.dim_oc = self.env.ref("etp_assessment_pro.dim_image_oc")

    def _master_opt(self, master_dim, label):
        return master_dim.option_ids.filtered(lambda o: o.name == label)[:1]

    def _attach_dim(self, question, master_dim, correct_label):
        qd = self.QDim.create({
            "question_id": question.id,
            "dimension_id": master_dim.id,
        })
        for line in qd.option_line_ids:
            line.is_correct = line.master_option_id.name == correct_label
        return qd

    def _build_image_ab(self, official):
        q = self.Question.create({
            "name": "AB Eval",
            "prompt": "Compare Response A and Response B.",
            "question_type": "image_ab",
            "official_reasoning": "A follows the instruction and is sharper.",
        })
        for master_dim, label in official:
            self._attach_dim(q, master_dim, label)
        self.env["etp.assessment.pro.question.image"].create([
            {"question_id": q.id, "label": "Response A", "slot": "a"},
            {"question_id": q.id, "label": "Response B", "slot": "b"},
        ])
        return q

    def _evaluator(self):
        applicant = self.Applicant.create({
            "partner_name": "Cand", "email_from": "cand@example.com"})
        assessment = self.Assessment.create({"name": "Img Assessment"})
        return self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
        }), applicant, assessment


class TestImageModelAndSeed(_Base):
    def test_image_model_and_seed(self):
        for d in (self.dim_if, self.dim_vq, self.dim_lai, self.dim_oc):
            names = d.option_ids.mapped("name")
            for label in ("Response A", "Response B", "Both Good",
                          "Both Bad", "Tie"):
                self.assertIn(label, names)
        q = self._build_image_ab([
            (self.dim_if, "Response A"),
            (self.dim_vq, "Response B"),
            (self.dim_lai, "Response A"),
            (self.dim_oc, "Response A"),
        ])
        self.assertEqual(len(q.image_ids), 2)
        self.assertEqual(set(q.image_ids.mapped("slot")), {"a", "b"})
        self.assertEqual(len(q.question_dimension_ids), 4)


class TestImageAbObjectiveScore(_Base):
    def test_image_ab_has_no_code_objective_pool(self):
        # EQUAL MARKS: image_ab is graded as a single LLM mark, NOT a
        # code-objective partial-credit pool. So _compute_score yields 0/0 and
        # the response needs_llm (the axis picks are LLM grading input).
        q = self._build_image_ab([
            (self.dim_if, "Response A"),
            (self.dim_vq, "Response B"),
            (self.dim_lai, "Response A"),
            (self.dim_oc, "Response A"),
        ])
        ev, applicant, assessment = self._evaluator()
        picks = [
            (self.dim_if, "Response A"),
            (self.dim_vq, "Response A"),
            (self.dim_lai, "Response A"),
            (self.dim_oc, "Response A"),
        ]
        line_vals = [
            (0, 0, {
                "dimension_id": master.id,
                "selected_option_id": self._master_opt(master, label).id,
            })
            for master, label in picks
        ]
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
            (self.dim_if, "Response A"), (self.dim_vq, "Response B"),
            (self.dim_lai, "Response A"), (self.dim_oc, "Response A")])
        q2 = self._build_image_ab([
            (self.dim_if, "Response A"), (self.dim_vq, "Response B"),
            (self.dim_lai, "Response A"), (self.dim_oc, "Response A")])
        ev, applicant, assessment = self._evaluator()
        picks = [(self.dim_if, "Response A"), (self.dim_vq, "Response A"),
                 (self.dim_lai, "Response A"), (self.dim_oc, "Response A")]
        lines = [(0, 0, {"dimension_id": m.id,
                         "selected_option_id": self._master_opt(m, l).id})
                 for m, l in picks]
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


class TestImageTextScorer(_Base):
    def _image_text_response(self):
        q = self.Question.create({
            "name": "Describe",
            "prompt": "Describe the image.",
            "question_type": "image_text",
            "subjective_rubric_json": json.dumps({
                "ideal_answer": "A fluffy cat sitting on a sofa.",
                "mandatory_elements": ["cat"],
                "penalty_rules": ["no hallucinated objects"],
                "scoring_guide": "Award points for accuracy.",
            }),
        })
        ev, applicant, assessment = self._evaluator()
        return self.Response.create({
            "assessment_id": assessment.id,
            "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id,
            "question_id": q.id,
            "justification": "A fluffy cat sitting on a sofa.",
        })

    def test_image_text_scorer_scaling(self):
        resp = self._image_text_response()
        fixed = json.dumps([{"id": resp.id, "score": 80, "feedback": "good"}])
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scored = scoring._score_image_text_items(self.env, resp)
        self.assertEqual(scored, 1)
        resp.invalidate_recordset()
        self.assertEqual(resp.llm_state, "scored")
        self.assertAlmostEqual(resp.llm_raw_score, 0.8)
        self.assertTrue(resp.llm_passed)
        # EQUAL MARKS: every question worth 1; pass earns the single mark.
        self.assertEqual(resp.llm_max_score, 1)
        self.assertEqual(resp.llm_score, 1)

    def test_image_text_scorer_fail_below_threshold(self):
        resp = self._image_text_response()
        fixed = json.dumps([{"id": resp.id, "score": 40, "feedback": "weak"}])
        with patch.object(vertex, "_call_vertex", return_value=fixed):
            scoring._score_image_text_items(self.env, resp)
        resp.invalidate_recordset()
        self.assertFalse(resp.llm_passed)
        self.assertEqual(resp.llm_score, 0)
        self.assertEqual(resp.llm_max_score, 1)


class TestImageAbScorerStub(_Base):
    def test_image_ab_scorer_scaling(self):
        q = self._build_image_ab([
            (self.dim_if, "Response A"),
            (self.dim_oc, "Response A"),
        ])
        ev, applicant, assessment = self._evaluator()
        assessment.require_justification_image_comparison = True  # blend path
        line_vals = [
            (0, 0, {
                "dimension_id": qd.dimension_id.id,
                "selected_option_id": self._master_opt(
                    qd.dimension_id, "Response A").id,
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
        fixed = json.dumps([{
            "id": resp.id, "score": 80, "alignment": "high",
            "strengths": ["clear"], "issues": [], "feedback": "solid",
        }])
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
            (self.dim_if, "Response A"),
            (self.dim_vq, "Response B"),
            (self.dim_lai, "Response A"),
            (self.dim_oc, "Response A"),
        ])
        ev, _applicant, _assessment = self._evaluator()
        form = {
            "question_id": str(q.id),
            "justification": "Response A is sharper with more detail.",
        }
        for master, label in [
            (self.dim_if, "Response A"),
            (self.dim_vq, "Response A"),
            (self.dim_lai, "Response A"),
            (self.dim_oc, "Response A"),
        ]:
            form["dimension_%d" % master.id] = str(
                self._master_opt(master, label).id)
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, None, form)
        self.assertTrue(resp)
        self.assertEqual(len(resp.line_ids), 4)
        self.assertTrue(resp.justification.startswith("Response A"))
        self.assertEqual(resp.state, "submitted")

    def test_record_response_image_text(self):
        q = self.Question.create({
            "name": "Prompt it",
            "prompt": "Write a generation prompt.",
            "question_type": "image_text",
            "subjective_rubric_json": json.dumps({"ideal_answer": "x"}),
        })
        ev, _applicant, _assessment = self._evaluator()
        form = {
            "question_id": str(q.id),
            "justification": "A photorealistic cat on a sofa, soft lighting.",
        }
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, None, form)
        self.assertTrue(resp)
        self.assertFalse(resp.line_ids)
        self.assertTrue(resp.justification.startswith("A photorealistic"))
        self.assertEqual(resp.state, "submitted")

    def test_record_response_image_text_requires_text(self):
        q = self.Question.create({
            "name": "Prompt it",
            "prompt": "Write a generation prompt.",
            "question_type": "image_text",
        })
        ev, _applicant, _assessment = self._evaluator()
        form = {"question_id": str(q.id), "justification": "  "}
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            resp = ctrl._record_response(ev, None, form)
        self.assertFalse(resp)


class TestImageAbBlendScoring(_Base):
    """image_ab = objective verdicts (code) + an optional LLM justification,
    blended ceil(0.75*verdict% + 0.25*justification%). The single mark follows
    the admin-configurable subjective threshold, read live from Settings."""

    OFFICIAL = [("dim_if", "Response A"), ("dim_vq", "Response B"),
                ("dim_lai", "Response A"), ("dim_oc", "Response A")]

    def _resp(self, assessment, ev, applicant, justification="", correct=3):
        official = [(getattr(self, d), lbl) for d, lbl in self.OFFICIAL]
        q = self._build_image_ab(official)
        picks = []
        for i, (m, lbl) in enumerate(official):
            picks.append((m, lbl) if i < correct
                         else (m, "Tie" if lbl != "Tie" else "Both Bad"))
        lines = [(0, 0, {"dimension_id": m.id,
                         "selected_option_id": self._master_opt(m, l).id})
                 for m, l in picks]
        return self.Response.create({
            "assessment_id": assessment.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": applicant.id, "question_id": q.id,
            "justification": justification, "line_ids": lines})

    def _decide_at(self, threshold, resp):
        """Set the live (config-driven) subjective threshold via Settings and let
        it re-decide already-scored answers, then read the fresh values."""
        self.env["res.config.settings"].create({
            "etp_assessment_pro_subjective_pass_threshold": float(threshold),
        }).set_values()
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
        r.write({"llm_raw_100": 80, "llm_state": "scored"})
        self.assertEqual(r.ab_final_pct, 77.0)           # ceil(0.75*75 + 0.25*80)

    def test_toggle_on_blank_justification_scores_verdicts_only(self):
        ev, applicant, a = self._evaluator()
        a.require_justification_image_comparison = True
        r = self._resp(a, ev, applicant, justification="", correct=3)
        r._enqueue_subjective_scoring()
        self.assertEqual(r.llm_state, "scored")          # blank -> no LLM
        self.assertEqual(r.ab_final_pct, 57.0)           # ceil(0.75*75)

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
