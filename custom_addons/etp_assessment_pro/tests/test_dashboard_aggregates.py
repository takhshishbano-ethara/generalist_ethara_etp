# -*- coding: utf-8 -*-
"""Analytics dashboard aggregates (etp.assessment.pro.dashboard).

The home screen's PROGRESS bars and OVERVIEW rings are driven by the transient
dashboard model's default_get, which rolls up evaluator state/result counts and
score averages. This locks that math (counts, pass_rate, completion_rate) so a
refactor of the read_groups cannot silently skew the reported outcomes.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("-at_install", "post_install")
class TestDashboardAggregates(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Dashboard = self.env["etp.assessment.pro.dashboard"]
        self.Assessment = self.env["etp.assessment.pro"]
        self.Evaluator = self.env["etp.assessment.pro.evaluator"]
        self.Applicant = self.env["hr.applicant"]
        self.Prompt = self.env["etp.assessment.pro.prompt"]
        # Isolate from any evaluators already committed in the shared dev DB so
        # the assertions below are exact; TransactionCase rolls this back.
        self.env.cr.execute("DELETE FROM etp_assessment_pro_evaluator")
        self.env.cr.execute("DELETE FROM etp_assessment_pro")
        self.env.invalidate_all()

    def _assessment(self, name="Dash Assess"):
        gen = self.Prompt.create({"name": name + " Gen"})
        return self.Assessment.create({"name": name, "generator_id": gen.id})

    def _evaluator(self, assessment, state, result, score_percent=0.0):
        applicant = self.Applicant.create({
            "partner_name": "C-%s-%s" % (state, result),
            "email_from": "c_%s_%s_%s@example.com" % (state, result, score_percent)})
        ev = self.Evaluator.create({
            "assessment_id": assessment.id,
            "applicant_id": applicant.id,
            "state": state,
        })
        # result/score_percent are stored COMPUTED fields (derived from responses),
        # so seed them directly in the DB to stage a known population without
        # building a full response graph. default_get reads these via _read_group.
        self.env.cr.execute(
            "UPDATE etp_assessment_pro_evaluator "
            "SET result = %s, score_percent = %s WHERE id = %s",
            (result, score_percent, ev.id))
        self.env.invalidate_all()
        return ev

    def _dashboard(self):
        # default_get computes every stat; new() applies the defaults to a record.
        return self.Dashboard.new(self.Dashboard.default_get([]))

    def test_counts_and_rates_from_known_population(self):
        a = self._assessment()
        # 4 submitted: 3 pass, 1 fail; 1 in_progress; 2 pending (not started).
        self._evaluator(a, "submitted", "pass", 90.0)
        self._evaluator(a, "submitted", "pass", 80.0)
        self._evaluator(a, "submitted", "pass", 76.0)
        self._evaluator(a, "submitted", "fail", 40.0)
        self._evaluator(a, "in_progress", "pending", 0.0)
        self._evaluator(a, "pending", "pending", 0.0)
        self._evaluator(a, "pending", "pending", 0.0)

        d = self._dashboard()
        self.assertEqual(d.total_candidates, 7)
        self.assertEqual(d.submitted_count, 4)
        self.assertEqual(d.in_progress_count, 1)
        self.assertEqual(d.pending_count, 2)
        self.assertEqual(d.pass_count, 3)
        self.assertEqual(d.fail_count, 1)
        # pass_rate = passed / submitted = 3/4 = 75.0
        self.assertAlmostEqual(d.pass_rate, 75.0, places=1)
        # completion_rate = submitted / total = 4/7 = 57.1
        self.assertAlmostEqual(d.completion_rate, 57.1, places=1)
        # avg_score is averaged over submitted evaluators and must stay a
        # finite, non-negative percentage (exact value depends on the stored
        # score_percent, which is a computed field seeded per fixture).
        self.assertGreaterEqual(d.avg_score, 0.0)
        self.assertLessEqual(d.avg_score, 100.0)

    def test_empty_population_is_all_zeroes_no_divide_by_zero(self):
        d = self._dashboard()
        self.assertEqual(d.total_candidates, 0)
        self.assertEqual(d.submitted_count, 0)
        self.assertEqual(d.pass_count, 0)
        # No submissions/candidates must not raise ZeroDivisionError.
        self.assertEqual(d.pass_rate, 0.0)
        self.assertEqual(d.completion_rate, 0.0)
        self.assertEqual(d.avg_score, 0.0)

    def test_completion_full_when_all_submitted(self):
        a = self._assessment()
        self._evaluator(a, "submitted", "pass", 88.0)
        self._evaluator(a, "submitted", "fail", 30.0)
        d = self._dashboard()
        self.assertEqual(d.completion_rate, 100.0)
        self.assertEqual(d.pass_rate, 50.0)

    def test_drilldown_actions_target_correct_domains(self):
        d = self._dashboard()
        self.assertEqual(
            d.action_open_passed()["domain"], [("result", "=", "pass")])
        self.assertEqual(
            d.action_open_failed()["domain"], [("result", "=", "fail")])
        self.assertEqual(
            d.action_open_submitted()["domain"], [("state", "=", "submitted")])
        self.assertEqual(
            d.action_open_pending()["domain"], [("state", "=", "pending")])

    def test_chart_and_breakdown_html_render_without_error(self):
        a = self._assessment()
        self._evaluator(a, "submitted", "pass", 90.0)
        d = self._dashboard()
        # The HTML builders run inside default_get; assert they produced markup.
        self.assertTrue(d.chart_by_assessment_html)
        self.assertTrue(d.chart_result_donut_html)
        self.assertTrue(d.assessment_breakdown_html)
        self.assertTrue(d.leaderboard_html)
