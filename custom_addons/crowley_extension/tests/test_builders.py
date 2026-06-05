from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.crowley_extension.controllers.analytics_dashboard import (
    _build_failed_task,
    _build_review_state,
    _build_status_chart_aligned,
    _build_total_task,
)
from odoo.addons.crowley_extension.controllers.dashboard_overview import (
    STAGE_BUCKETS,
    _compute_task_progress,
)
from odoo.addons.crowley_extension.controllers.task_view_dashboard import (
    _serialize_task,
)


@tagged("post_install", "-at_install", "crowley_extension")
class TestControllerBuilders(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gen = cls.env["crowley.generation"]
        cls.user = cls.env["res.users"].create({
            "name": "Crowley Builder Tester",
            "login": "crowley_builder_tester",
            "email": "crowley_builder_tester@example.com",
        })
        cls.scope = [("user_id", "=", cls.user.id)]
        cls.filters = {
            "start": None,
            "end": None,
            "month_start": None,
            "month_end": None,
        }

    def _make_gen(self, **vals):
        defaults = {
            "prompt": "Builder fixture prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "user_id": self.user.id,
        }
        defaults.update(vals)
        return self.Gen.create(defaults)

    def _set_state(self, gen, state=None, review_state=None, cost_usd=None):
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
        if not assignments:
            return
        params.append(gen.id)
        self.env.cr.execute(
            f"UPDATE crowley_generation SET {', '.join(assignments)} WHERE id=%s",
            params,
        )

    def test_build_total_task_shape(self):
        result = _build_total_task(self.env, self.scope, self.filters)
        self.assertIn("total_task_count", result)
        self.assertIn("current_period_count", result)
        self.assertIn("previous_period_count", result)
        self.assertIn("difference_percentage", result)
        self.assertIn("trend", result)
        self.assertIn("current_period", result)
        self.assertIn("previous_period", result)
        self.assertIn(result["trend"], ("increase", "decrease", "no_change"))

    def test_build_total_task_counts_match(self):
        self._make_gen()
        self._make_gen()
        self.Gen.invalidate_model()
        result = _build_total_task(self.env, self.scope, self.filters)
        self.assertEqual(result["total_task_count"], 2)

    def test_build_failed_task_zero_when_empty(self):
        result = _build_failed_task(self.env, self.scope, self.filters)
        self.assertEqual(result["failed_task_count"], 0)
        self.assertEqual(result["total_task_count"], 0)
        self.assertEqual(result["failure_percentage"], 0.0)

    def test_build_failed_task_counts_failed_state(self):
        g_ok = self._make_gen()
        g_fail = self._make_gen()
        self._set_state(g_fail, state="failed")
        self.Gen.invalidate_model()
        result = _build_failed_task(self.env, self.scope, self.filters)
        self.assertEqual(result["total_task_count"], 2)
        self.assertEqual(result["failed_task_count"], 1)
        self.assertEqual(result["failure_percentage"], 50.0)

    def test_build_status_chart_includes_all_selection_keys(self):
        result = _build_status_chart_aligned(self.env, self.scope, self.filters)
        chart_keys = {row["status_key"] for row in result["status_chart"]}
        selection_keys = {key for key, _ in self.Gen._fields["state"].selection}
        self.assertEqual(chart_keys, selection_keys)
        for row in result["status_chart"]:
            self.assertIn("status_name", row)
            self.assertIn("count", row)
            self.assertIn("percentage", row)

    def test_build_status_chart_counts_by_state(self):
        g_done = self._make_gen()
        self._set_state(g_done, state="done")
        g_failed = self._make_gen()
        self._set_state(g_failed, state="failed")
        self._make_gen()
        self.Gen.invalidate_model()
        result = _build_status_chart_aligned(self.env, self.scope, self.filters)
        self.assertEqual(result["total_task_count"], 3)
        by_key = {row["status_key"]: row["count"] for row in result["status_chart"]}
        self.assertEqual(by_key.get("done", 0), 1)
        self.assertEqual(by_key.get("failed", 0), 1)
        self.assertEqual(by_key.get("draft", 0), 1)

    def test_build_review_state_includes_all_selection_keys(self):
        result = _build_review_state(self.env, self.scope, self.filters)
        dist_keys = {row["verdict_key"] for row in result["distribution"]}
        selection_keys = {
            key for key, _ in self.Gen._fields["review_state"].selection
        }
        self.assertEqual(dist_keys, selection_keys)

    def test_build_review_state_counts_decisions(self):
        g_approved = self._make_gen()
        self._set_state(g_approved, state="done", review_state="approved")
        g_rejected = self._make_gen()
        self._set_state(g_rejected, state="done", review_state="rejected")
        self._make_gen()
        self.Gen.invalidate_model()
        result = _build_review_state(self.env, self.scope, self.filters)
        self.assertEqual(result["total_task_count"], 3)
        by_key = {row["verdict_key"]: row["count"] for row in result["distribution"]}
        self.assertEqual(by_key.get("approved", 0), 1)
        self.assertEqual(by_key.get("rejected", 0), 1)


@tagged("post_install", "-at_install", "crowley_extension")
class TestDashboardOverviewBuilders(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gen = cls.env["crowley.generation"]
        cls.user = cls.env["res.users"].create({
            "name": "Overview Builder Tester",
            "login": "crowley_overview_tester",
            "email": "crowley_overview_tester@example.com",
        })

    def _make_gen(self, **vals):
        defaults = {
            "prompt": "Overview fixture prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "user_id": self.user.id,
        }
        defaults.update(vals)
        return self.Gen.create(defaults)

    def _set_state(self, gen, state=None, review_state=None):
        assignments = []
        params = []
        if state is not None:
            assignments.append("state=%s")
            params.append(state)
        if review_state is not None:
            assignments.append("review_state=%s")
            params.append(review_state)
        if not assignments:
            return
        params.append(gen.id)
        self.env.cr.execute(
            f"UPDATE crowley_generation SET {', '.join(assignments)} WHERE id=%s",
            params,
        )

    def test_task_progress_shape(self):
        scope = [("user_id", "=", self.user.id)]
        result = _compute_task_progress(self.env, scope)
        self.assertIn("label", result)
        self.assertIn("total", result)
        self.assertIn("count", result)
        self.assertIn("items", result)
        self.assertEqual(len(result["items"]), len(STAGE_BUCKETS))
        item_keys = {item["key"] for item in result["items"]}
        expected_keys = {key for key, _, _ in STAGE_BUCKETS}
        self.assertEqual(item_keys, expected_keys)

    def test_task_progress_counts_per_bucket(self):
        scope = [("user_id", "=", self.user.id)]
        self._make_gen()
        g_queued = self._make_gen()
        self._set_state(g_queued, state="queued")
        g_done_pending = self._make_gen()
        self._set_state(g_done_pending, state="done")
        g_approved = self._make_gen()
        self._set_state(g_approved, state="done", review_state="approved")
        g_failed = self._make_gen()
        self._set_state(g_failed, state="failed")
        self.Gen.invalidate_model()

        result = _compute_task_progress(self.env, scope)
        self.assertEqual(result["total"], 5)
        by_key = {item["key"]: item["value"] for item in result["items"]}
        self.assertEqual(by_key["draft"], 1)
        self.assertEqual(by_key["generating"], 1)
        self.assertEqual(by_key["pending_review"], 1)
        self.assertEqual(by_key["approved"], 1)
        self.assertEqual(by_key["rejected_failed"], 1)


@tagged("post_install", "-at_install", "crowley_extension")
class TestTaskSerializer(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gen = cls.env["crowley.generation"]
        cls.user = cls.env["res.users"].create({
            "name": "Serializer Tester",
            "login": "crowley_serializer_tester",
            "email": "crowley_serializer_tester@example.com",
        })

    def _make_gen(self, **vals):
        defaults = {
            "prompt": "Serializer fixture prompt",
            "duration": "5",
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "user_id": self.user.id,
        }
        defaults.update(vals)
        return self.Gen.create(defaults)

    def test_serialize_draft_row(self):
        gen = self._make_gen()
        row = _serialize_task(gen)
        self.assertEqual(row["id"], gen.id)
        self.assertEqual(row["state"], "draft")
        self.assertEqual(row["stage"], "s1_draft")
        self.assertEqual(row["stage_label"], "S1 Draft")
        self.assertEqual(row["status"], "unstarted")
        self.assertEqual(row["assigned_ql_id"], self.user.id)
        self.assertEqual(row["assigned_ql_name"], self.user.name)
        self.assertEqual(row["spec"], "720p · 16:9 · 5s")
        self.assertIsInstance(row["cost_usd"], float)

    def test_serialize_done_approved_row(self):
        gen = self._make_gen()
        self.env.cr.execute(
            "UPDATE crowley_generation SET state=%s, review_state=%s WHERE id=%s",
            ("done", "approved", gen.id),
        )
        self.Gen.invalidate_model()
        gen = self.Gen.browse(gen.id)
        row = _serialize_task(gen)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["review_state"], "approved")
        self.assertEqual(row["stage"], "s2_qc")
        self.assertEqual(row["status"], "approved")

    def test_serialize_failed_row(self):
        gen = self._make_gen()
        self.env.cr.execute(
            "UPDATE crowley_generation SET state=%s WHERE id=%s",
            ("failed", gen.id),
        )
        self.Gen.invalidate_model()
        gen = self.Gen.browse(gen.id)
        row = _serialize_task(gen)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["stage"], "failed")
        self.assertEqual(row["status"], "failed_qc")

    def test_serialize_returns_all_expected_keys(self):
        gen = self._make_gen()
        row = _serialize_task(gen)
        expected = {
            "id",
            "seq",
            "spec",
            "raw_prompt",
            "golden_prompt",
            "is_enriched",
            "assigned_ql_id",
            "assigned_ql_name",
            "category_slug",
            "category",
            "stage",
            "stage_label",
            "status",
            "state",
            "review_state",
            "cost_usd",
            "attempts_used",
            "attempts_remaining",
            "updated_at",
            "created_at",
        }
        self.assertEqual(set(row.keys()), expected)
