"""Shape-certification tests for the vegeta_extension dashboard payloads.

These lock the exact JSON contracts the Flutter project-detail screen probes
for, so the "displays like crowley" wiring can't silently regress:

  * Overview  — InternalOverviewResponseModel.matches() requires
    ``overview.kpi`` (with ``items``), ``overview.task_progress`` and
    ``overview.approved_per_week``, each item carrying a fixed field set.
  * Analytics — InternalAnalyticsResponseModel.matches() requires the
    top-level ``spend_by_category`` / ``qc_pass_rate_by_ql`` / ``daily_burn_rate``
    sections, each with the field sets the Internal*Model parsers read.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.vegeta_extension.controllers.analytics_dashboard import (
    _build_analytics,
    _build_spend_by_category,
    _range_domain,
    _resolve_context,
)
from odoo.addons.vegeta_extension.controllers.dashboard_overview import (
    _compute_approved_per_week,
    _compute_kpi,
    _compute_task_progress,
)

# Field sets the Flutter Internal*Model.fromJson factories read verbatim.
KPI_ITEM_KEYS = {"key", "label", "value", "sub_string", "pattern", "sign"}
TASK_PROGRESS_ITEM_KEYS = {"key", "label", "value", "percentage"}
APPROVED_WEEK_ITEM_KEYS = {
    "key",
    "label",
    "value",
    "week_start",
    "week_end",
    "delta_vs_prev_week",
    "pattern",
    "sign",
}
SPEND_ITEM_KEYS = {
    "key",
    "label",
    "value",
    "amount",
    "percentage",
    "color_token",
}
QC_ROW_KEYS = {
    "key",
    "label",
    "initials",
    "value",
    "pass_rate",
    "tasks",
    "reviewed",
    "taskers",
    "color_token",
}
DAILY_BURN_KEYS = {
    "title",
    "sub_title",
    "type",
    "headline",
    "headline_caption",
    "legend",
    "data",
}


@tagged("post_install", "-at_install", "vegeta_extension")
class TestVegetaDashboardBuilders(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["vegeta.job"]
        cls.user = cls.env["res.users"].create({
            "name": "Vegeta Builder Tester",
            "login": "vegeta_builder_tester",
            "email": "vegeta_builder_tester@example.com",
        })
        cls.scope = [("user_id", "=", cls.user.id)]

    def _make_job(self, **vals):
        defaults = {"url": "https://example.com", "user_id": self.user.id}
        defaults.update(vals)
        return self.Job.create(defaults)

    def _set_fields(self, job, **cols):
        """Set state/qc_verdict/score/cost/completed_at via SQL to avoid the
        model's action-driven pipeline side effects."""
        cols = {k: v for k, v in cols.items() if v is not None}
        if not cols:
            return
        assignments = ", ".join(f"{name}=%s" for name in cols)
        params = list(cols.values()) + [job.id]
        self.env.cr.execute(
            f"UPDATE vegeta_job SET {assignments} WHERE id=%s", params
        )
        job.invalidate_recordset()

    def _ctx(self):
        today = fields.Datetime.now().date()
        rng = {"key": "30d", "start": today - timedelta(days=29), "end": today}
        return {
            "project": self.env["project.project"].browse(),
            "role": "admin",
            "rng": rng,
            "taskers": self.env["hr.employee"].browse(),
            "user_ids": [self.user.id],
            "ql_of_user": {self.user.id: 0},
            "ql_name": {0: "Unassigned"},
            "ql_taskers": {0: {self.user.id}},
            "scope": self.scope + _range_domain(rng),
        }

    # ── Overview contract ──

    def test_compute_kpi_shape(self):
        kpi = _compute_kpi(self.env, self.scope)
        self.assertIn("count", kpi)
        self.assertIn("items", kpi)
        self.assertTrue(kpi["items"], "KPI must always emit items")
        by_key = {item["key"]: item for item in kpi["items"]}
        keys = set(by_key)
        # Every key referenced by KPI_KEYS_BY_VIEW must be computed, including
        # crowley's `total_burned` (kept for parity) and the vegeta `avg_score`.
        self.assertGreaterEqual(
            keys,
            {
                "total_burned",
                "avg_score",
                "active_tasks",
                "approval_rate",
                "team_members",
                "total_tasks_done",
                "qc_pass_rate",
                "approved_today",
            },
        )
        # total_burned is kept but empty (no cost data) ⇒ value "" ⇒ renders "—".
        self.assertEqual(by_key["total_burned"]["value"], "")
        for item in kpi["items"]:
            self.assertEqual(set(item) , KPI_ITEM_KEYS)
            self.assertIsInstance(item["value"], str)

    def test_compute_task_progress_shape(self):
        result = _compute_task_progress(self.env, self.scope)
        self.assertEqual(
            set(result), {"label", "total", "count", "items"}
        )
        self.assertTrue(result["items"])
        for item in result["items"]:
            self.assertEqual(set(item), TASK_PROGRESS_ITEM_KEYS)
            self.assertIsInstance(item["value"], int)
            self.assertIsInstance(item["percentage"], float)

    def test_task_progress_counts_buckets(self):
        self._set_fields(self._make_job(), state="done")
        self._set_fields(self._make_job(), state="failed")
        self._make_job()  # not_assigned → draft bucket
        result = _compute_task_progress(self.env, self.scope)
        by_key = {item["key"]: item["value"] for item in result["items"]}
        self.assertEqual(by_key["done"], 1)
        self.assertEqual(by_key["failed"], 1)
        self.assertEqual(by_key["draft"], 1)
        self.assertEqual(result["total"], 3)

    def test_compute_approved_per_week_shape(self):
        result = _compute_approved_per_week(self.env, self.scope, 6)
        self.assertEqual(
            set(result),
            {"label", "sub_string", "total", "count", "items"},
        )
        self.assertEqual(len(result["items"]), 6)
        for item in result["items"]:
            self.assertEqual(set(item), APPROVED_WEEK_ITEM_KEYS)

    # ── Analytics contract ──

    def test_build_analytics_top_level_shape(self):
        data = _build_analytics(self.env, self._ctx())
        # The three keys the Flutter analytics probe keys off must be present.
        self.assertIn("spend_by_category", data)
        self.assertIn("qc_pass_rate_by_ql", data)
        self.assertIn("daily_burn_rate", data)
        self.assertIn("kpi", data)
        self.assertIn("role", data)

    def test_kpi_v2_items_shape(self):
        data = _build_analytics(self.env, self._ctx())
        kpi = data["kpi"]
        self.assertIn("items", kpi)
        for item in kpi["items"]:
            self.assertEqual(set(item), KPI_ITEM_KEYS)

    def test_spend_by_category_shape(self):
        sbc = _build_analytics(self.env, self._ctx())["spend_by_category"]
        self.assertEqual(
            set(sbc), {"title", "sub_title", "type", "total", "items"}
        )
        for item in sbc["items"]:
            self.assertEqual(set(item), SPEND_ITEM_KEYS)

    def test_qc_pass_rate_by_ql_shape(self):
        qc = _build_analytics(self.env, self._ctx())["qc_pass_rate_by_ql"]
        self.assertEqual(set(qc), {"title", "sub_title", "type", "items"})
        for row in qc["items"]:
            self.assertEqual(set(row), QC_ROW_KEYS)

    def test_daily_burn_rate_shape(self):
        dbr = _build_analytics(self.env, self._ctx())["daily_burn_rate"]
        self.assertEqual(set(dbr), DAILY_BURN_KEYS)
        self.assertIsInstance(dbr["legend"], list)
        self.assertIsInstance(dbr["data"], list)

    def test_spend_by_category_aggregates_cost(self):
        ctx = self._ctx()
        job = self._make_job(category_id=self.env["vegeta.category"].create({
            "name": "Retail",
        }).id)
        self._set_fields(job, llm_qc_cost_usd=12.5)
        sbc = _build_spend_by_category(self.env, ctx)
        self.assertAlmostEqual(sbc["total"], 12.5, places=2)
        self.assertTrue(sbc["items"])
        self.assertEqual(sbc["items"][0]["label"], "Retail")

    def test_resolve_context_requires_project_id(self):
        ctx, error = _resolve_context(self.env, {})
        self.assertIsNone(ctx)
        self.assertIsNotNone(error)

    # ── Empty-when-no-data convention (cost is unpopulated in vegeta) ──

    def test_spend_by_category_empty_without_cost(self):
        # A job with a category but no cost must NOT surface a $0 row — the
        # spend card hides when there is no real spend.
        ctx = self._ctx()
        cat = self.env["vegeta.category"].create({"name": "CRM"})
        self._make_job(category_id=cat.id)  # llm_qc_cost_usd defaults to 0.0
        sbc = _build_spend_by_category(self.env, ctx)
        self.assertEqual(sbc["items"], [], "no spend ⇒ no spend-by-category rows")
        self.assertEqual(sbc["total"], 0.0)
        # Key set stays intact for schema parity.
        self.assertEqual(
            set(sbc), {"title", "sub_title", "type", "total", "items"}
        )

    def test_daily_burn_rate_empty_without_cost(self):
        ctx = self._ctx()
        self._make_job()  # no cost
        dbr = _build_analytics(self.env, ctx)["daily_burn_rate"]
        self.assertEqual(dbr["data"], [], "no spend ⇒ no burn series ⇒ card hides")
        self.assertEqual(dbr["legend"], [])
        self.assertEqual(set(dbr), DAILY_BURN_KEYS)  # key intact

    def test_analytics_kpi_keeps_crowley_keys_empty_without_cost(self):
        # Crowley keys are kept (not omitted); cost/token cards carry empty
        # values since vegeta does not track them.
        ctx = self._ctx()
        self._make_job()
        kpi = _build_analytics(self.env, ctx)["kpi"]
        by_key = {item["key"]: item for item in kpi["items"]}
        self.assertIn("total_spend", by_key)
        self.assertEqual(by_key["total_spend"]["value"], "")
        self.assertIn("avg_tokens_per_task", by_key)
        self.assertEqual(by_key["avg_tokens_per_task"]["value"], "")
        self.assertIn("total_tasks", by_key)  # vegeta addition

    def test_analytics_kpi_shows_total_spend_with_cost(self):
        # Self-heal: once cost exists, the Total Spend card fills with a figure.
        ctx = self._ctx()
        self._set_fields(self._make_job(), llm_qc_cost_usd=4.0)
        kpi = _build_analytics(self.env, ctx)["kpi"]
        by_key = {item["key"]: item for item in kpi["items"]}
        self.assertIn("total_spend", by_key)
        self.assertNotEqual(by_key["total_spend"]["value"], "")
