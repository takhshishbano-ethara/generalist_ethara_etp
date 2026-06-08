from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.crowley_sourcing_extension.controllers.dashboard_overview import (
    ACCEPTED_DAYS,
    BURN_DAYS,
    STAGE_BUCKETS,
    _compute_accepted_per_day,
    _compute_approved_per_week,
    _compute_budget,
    _compute_burn_rate,
    _compute_kpi,
    _compute_recent_activity,
    _compute_task_progress,
)
from odoo.addons.crowley_sourcing_extension.controllers.logs import (
    SOURCES,
    _job_entries,
    _notification_entries,
    _processing_log_entries,
    _project_scope_domain,
)


@tagged("post_install", "-at_install", "crowley_sourcing_extension")
class TestSourcingOverviewBuilders(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create({
            "name": "Sourcing Overview Tester",
            "login": "crowley_sourcing_overview_tester",
            "email": "crowley_sourcing_overview_tester@example.com",
        })
        cls.scope = [("assigned_to", "=", cls.user.id)]

    def test_kpi_includes_all_role_keys(self):
        result = _compute_kpi(self.env, self.scope)
        item_keys = {item["key"] for item in result["items"]}
        expected_keys = {
            "total_burned",
            "active_tasks",
            "approval_rate",
            "team_members",
            "tasks_done_qc",
            "total_qc_done",
            "force_submit_rate",
            "qc_pending",
            "total_tasks_done",
            "total_qc_approved",
            "my_qc_pass_ratio",
            "tasks_done_today",
        }
        self.assertEqual(item_keys, expected_keys)
        self.assertIsInstance(result["count"], str)
        for item in result["items"]:
            self.assertEqual(
                set(item.keys()),
                {"key", "label", "value", "sub_string", "pattern", "sign"},
            )

    def test_task_progress_is_stage_funnel(self):
        result = _compute_task_progress(self.env, self.scope)
        self.assertEqual(result["label"], "Stage Funnel")
        self.assertEqual(len(result["items"]), len(STAGE_BUCKETS))
        self.assertEqual(
            [i["key"] for i in result["items"]], ["draft", "processed", "done"]
        )
        self.assertIsInstance(result["count"], str)
        for item in result["items"]:
            self.assertEqual(
                set(item.keys()), {"key", "label", "value", "percentage"}
            )
        self.assertIn("conversion_pct", result)
        self.assertIn("rejected_rework", result)

    def test_approved_per_week_matches_crowley_format(self):
        result = _compute_approved_per_week(self.env, self.scope, 6)
        self.assertEqual(
            set(result.keys()),
            {"label", "sub_string", "total", "count", "items"},
        )
        self.assertEqual(len(result["items"]), 6)
        for item in result["items"]:
            self.assertEqual(
                set(item.keys()),
                {
                    "key",
                    "label",
                    "value",
                    "week_start",
                    "week_end",
                    "delta_vs_prev_week",
                    "pattern",
                    "sign",
                },
            )

    def test_budget_shape(self):
        result = _compute_budget(self.env, self.scope)
        self.assertEqual(
            set(result.keys()),
            {
                "title",
                "cap",
                "spent",
                "spent_pct",
                "remaining",
                "remaining_pct",
                "avg_cost_per_accepted_pair",
                "projected_exhaustion_date",
            },
        )

    def test_burn_rate_shape(self):
        result = _compute_burn_rate(self.env, self.scope)
        self.assertEqual(len(result["data"]), BURN_DAYS)
        self.assertIn("today", result)
        self.assertIn("avg_7d", result)
        self.assertIn("peak", result)

    def test_accepted_per_day_stacked_by_category(self):
        result = _compute_accepted_per_day(self.env, self.scope)
        self.assertEqual(result["type"], "stacked_bar")
        self.assertEqual(len(result["data"]), ACCEPTED_DAYS)
        # "Other" bucket is always present in the legend.
        self.assertIn("other", {leg["key"] for leg in result["legend"]})
        for leg in result["legend"]:
            self.assertEqual(set(leg.keys()), {"key", "label", "color_token"})
        legend_keys = {leg["key"] for leg in result["legend"]}
        for row in result["data"]:
            self.assertEqual(set(row.keys()), {"date", "label", "total", "segments"})
            for seg in row["segments"]:
                self.assertEqual(set(seg.keys()), {"key", "value"})
                self.assertIn(seg["key"], legend_keys)

    def test_recent_activity_shape(self):
        result = _compute_recent_activity(self.env, self.scope)
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["items"], list)


@tagged("post_install", "-at_install", "crowley_sourcing_extension")
class TestSourcingLogsBuilders(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].create({
            "name": "Sourcing Logs Tester",
            "login": "crowley_sourcing_logs_tester",
            "email": "crowley_sourcing_logs_tester@example.com",
        })
        cls.scope = [("assigned_to", "=", cls.user.id)]

    def test_sources_constant(self):
        self.assertEqual(
            set(SOURCES), {"processing_log", "job", "notification"}
        )

    def test_project_scope_domain_rewrite(self):
        rewritten = _project_scope_domain(self.scope)
        self.assertEqual(rewritten, [("project_id.assigned_to", "=", self.user.id)])

    def test_processing_log_entries_shape(self):
        entries, capped = _processing_log_entries(self.env, [], self.scope)
        self.assertIsInstance(entries, list)
        self.assertIsInstance(capped, bool)

    def test_job_entries_shape(self):
        entries, capped = _job_entries(self.env, [], self.scope)
        self.assertIsInstance(entries, list)
        self.assertIsInstance(capped, bool)

    def test_notification_entries_shape(self):
        entries, capped = _notification_entries(self.env, [], "full")
        self.assertIsInstance(entries, list)
        self.assertIsInstance(capped, bool)
