from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "crowley_extension")
class TestPerformanceMetrics(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gen = cls.env["crowley.generation"]
        cls.user = cls.env["res.users"].create({
            "name": "Crowley Metrics Tester",
            "login": "crowley_metrics_tester",
            "email": "crowley_metrics_tester@example.com",
        })

    def _make_gen(self, **vals):
        defaults = {
            "prompt": "Performance metrics fixture prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "user_id": self.user.id,
        }
        defaults.update(vals)
        return self.Gen.create(defaults)

    def _set_state(self, gen, state=None, review_state=None, cost_usd=None, duration_seconds=None):
        assignments = []
        params = []
        if state is not None:
            assignments.append("state=%s")
            params.append(state)
        if review_state is not None:
            assignments.append("review_state=%s")
            params.append(review_state)
        if cost_usd is not None:
            assignments.append("cost_usd=%s")
            params.append(cost_usd)
        if duration_seconds is not None:
            assignments.append("duration_seconds=%s")
            params.append(duration_seconds)
        if not assignments:
            return
        params.append(gen.id)
        self.env.cr.execute(
            f"UPDATE crowley_generation SET {', '.join(assignments)} WHERE id=%s",
            params,
        )

    def test_scope_domain_no_role_returns_self_only(self):
        domain = self.Gen.with_user(self.user)._performance_scope_domain()
        self.assertEqual(domain, [("user_id", "=", self.user.id)])

    def test_metrics_with_no_data(self):
        metrics = self.Gen.with_user(self.user).get_performance_metrics()
        self.assertEqual(metrics["total_task_count"], 0)
        self.assertEqual(metrics["task_done"], 0)
        self.assertEqual(metrics["approved_count"], 0)
        self.assertEqual(metrics["rework_count"], 0)
        self.assertEqual(metrics["reviewed_count"], 0)
        self.assertEqual(metrics["approval_percentage"], 0.0)
        self.assertEqual(metrics["rework_percentage"], 0.0)
        self.assertEqual(metrics["aht_measured_count"], 0)
        self.assertEqual(metrics["avg_handling_time_seconds"], 0.0)
        self.assertEqual(metrics["avg_handling_time_minutes"], 0.0)
        self.assertEqual(metrics["cost_measured_count"], 0)
        self.assertEqual(metrics["total_cost_usd"], 0.0)
        self.assertEqual(metrics["average_cost_usd"], 0.0)

    def test_metrics_returns_all_expected_keys(self):
        metrics = self.Gen.with_user(self.user).get_performance_metrics()
        expected = {
            "total_task_count",
            "task_done",
            "approval_percentage",
            "rework_percentage",
            "avg_handling_time_seconds",
            "avg_handling_time_minutes",
            "approved_count",
            "rework_count",
            "reviewed_count",
            "aht_measured_count",
            "total_cost_usd",
            "average_cost_usd",
            "cost_measured_count",
        }
        self.assertEqual(set(metrics.keys()), expected)

    def test_metrics_aggregates_seeded_generations(self):
        g_approved = self._make_gen()
        self._set_state(
            g_approved,
            state="done",
            review_state="approved",
            cost_usd=0.5,
            duration_seconds=30,
        )
        g_rejected = self._make_gen()
        self._set_state(
            g_rejected,
            state="done",
            review_state="rejected",
            cost_usd=0.7,
            duration_seconds=45,
        )
        self._make_gen()
        self.Gen.invalidate_model()

        metrics = self.Gen.with_user(self.user).get_performance_metrics()
        self.assertEqual(metrics["total_task_count"], 3)
        self.assertEqual(metrics["task_done"], 2)
        self.assertEqual(metrics["approved_count"], 1)
        self.assertEqual(metrics["rework_count"], 1)
        self.assertEqual(metrics["reviewed_count"], 2)
        self.assertEqual(metrics["approval_percentage"], 50.0)
        self.assertEqual(metrics["rework_percentage"], 50.0)
        self.assertEqual(metrics["aht_measured_count"], 2)
        self.assertAlmostEqual(metrics["avg_handling_time_seconds"], 37.5, places=2)
        self.assertEqual(metrics["cost_measured_count"], 2)
        self.assertAlmostEqual(metrics["total_cost_usd"], 1.2, places=4)
        self.assertAlmostEqual(metrics["average_cost_usd"], 0.6, places=4)

    def test_metrics_only_counts_own_jobs_for_non_role_user(self):
        other = self.env["res.users"].create({
            "name": "Other Owner",
            "login": "crowley_metrics_other",
            "email": "crowley_metrics_other@example.com",
        })
        mine = self._make_gen()
        self._set_state(mine, state="done", cost_usd=0.25, duration_seconds=10)
        theirs = self._make_gen(user_id=other.id)
        self._set_state(theirs, state="done", cost_usd=99.0, duration_seconds=120)
        self.Gen.invalidate_model()

        metrics = self.Gen.with_user(self.user).get_performance_metrics()
        self.assertEqual(metrics["total_task_count"], 1)
        self.assertEqual(metrics["task_done"], 1)
        self.assertAlmostEqual(metrics["total_cost_usd"], 0.25, places=4)
