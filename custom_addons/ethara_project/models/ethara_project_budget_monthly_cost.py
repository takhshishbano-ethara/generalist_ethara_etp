import calendar
import logging
from datetime import date

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class EtharaProjectBudgetMonthlyCost(models.Model):
    _name = "ethara.project.budget.monthly.cost"
    _description = "Ethara Project Budget Monthly Cost Rollup"
    _order = "budget_id, period_month desc, basis"

    budget_id = fields.Many2one(
        "ethara.project.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ethara_project_id = fields.Many2one(
        "ethara.project",
        string="Project",
        related="budget_id.ethara_project_id",
        store=True,
        index=True,
    )
    period_month = fields.Date(
        string="Month",
        required=True,
        index=True,
        help="First day of the month this row represents.",
    )
    month_label = fields.Char(
        string="Month",
        compute="_compute_month_label",
        store=True,
    )
    basis = fields.Selection(
        [
            ("infra", "Infrastructure"),
            ("subscription", "Subscription"),
            ("consumed", "Consumed (Actual)"),
        ],
        string="Basis",
        required=True,
    )
    monthly_cost = fields.Float(string="Monthly Cost (USD)")
    per_day_avg_cost = fields.Float(
        string="Per Day Avg Cost (USD)",
        compute="_compute_per_day_avg_cost",
        store=True,
    )

    _sql_constraints = [
        (
            "budget_month_basis_uniq",
            "unique(budget_id, period_month, basis)",
            "A monthly rollup row already exists for this budget, month and basis.",
        ),
    ]

    @api.depends("period_month")
    def _compute_month_label(self):
        for rec in self:
            rec.month_label = (
                rec.period_month.strftime("%B %Y") if rec.period_month else ""
            )

    @api.depends("monthly_cost", "period_month")
    def _compute_per_day_avg_cost(self):
        for rec in self:
            if rec.period_month:
                days = calendar.monthrange(
                    rec.period_month.year, rec.period_month.month
                )[1]
                rec.per_day_avg_cost = (rec.monthly_cost or 0.0) / days
            else:
                rec.per_day_avg_cost = 0.0

    def _upsert_row(self, budget_id, period_month, basis, amount):
        existing = self.sudo().search(
            [
                ("budget_id", "=", budget_id),
                ("period_month", "=", period_month),
                ("basis", "=", basis),
            ],
            limit=1,
        )
        if existing:
            existing.write({"monthly_cost": amount})
        else:
            self.sudo().create(
                {
                    "budget_id": budget_id,
                    "period_month": period_month,
                    "basis": basis,
                    "monthly_cost": amount,
                }
            )

    def _rollup_budget(self, budget):
        if not budget:
            return
        months = set()

        infra_by_month = {}
        for line in budget.infra_line_ids:
            start = line.start_date
            end = line.end_date
            if not start or not end or end < start:
                continue
            per_day = line.per_day_cost or 0.0
            cursor = date(start.year, start.month, 1)
            end_month = date(end.year, end.month, 1)
            while cursor <= end_month:
                y, m = cursor.year, cursor.month
                month_days = calendar.monthrange(y, m)[1]
                month_start = date(y, m, 1)
                month_end = date(y, m, month_days)
                span_start = max(start, month_start)
                span_end = min(end, month_end)
                days = (span_end - span_start).days + 1
                if days > 0:
                    infra_by_month[month_start] = (
                        infra_by_month.get(month_start, 0.0) + per_day * days
                    )
                    months.add(month_start)
                if m == 12:
                    cursor = date(y + 1, 1, 1)
                else:
                    cursor = date(y, m + 1, 1)

        sub_by_month = {}
        for line in budget.subscription_line_ids:
            start = line.start_date
            end = line.end_date
            if not start or not end or end < start:
                continue
            per_day = line.per_day_cost or 0.0
            cursor = date(start.year, start.month, 1)
            end_month = date(end.year, end.month, 1)
            while cursor <= end_month:
                y, m = cursor.year, cursor.month
                month_days = calendar.monthrange(y, m)[1]
                month_start = date(y, m, 1)
                month_end = date(y, m, month_days)
                span_start = max(start, month_start)
                span_end = min(end, month_end)
                days = (span_end - span_start).days + 1
                if days > 0:
                    sub_by_month[month_start] = (
                        sub_by_month.get(month_start, 0.0) + per_day * days
                    )
                    months.add(month_start)
                if m == 12:
                    cursor = date(y + 1, 1, 1)
                else:
                    cursor = date(y, m + 1, 1)

        consumed_by_month = {}
        cost_domain = [
            ("budget_id", "=", budget.id),
            ("granularity", "=", "day"),
            ("is_model_breakdown", "=", False),
        ]
        for cl in self.env["ethara.project.cost.line"].sudo().search(cost_domain):
            if not cl.period:
                continue
            month_start = date(cl.period.year, cl.period.month, 1)
            consumed_by_month[month_start] = (
                consumed_by_month.get(month_start, 0.0) + (cl.amount_source or 0.0)
            )
            months.add(month_start)

        seen_keys = set()
        for m_start in sorted(months):
            self._upsert_row(budget.id, m_start, "infra", infra_by_month.get(m_start, 0.0))
            self._upsert_row(budget.id, m_start, "subscription", sub_by_month.get(m_start, 0.0))
            self._upsert_row(budget.id, m_start, "consumed", consumed_by_month.get(m_start, 0.0))
            seen_keys.add((m_start, "infra"))
            seen_keys.add((m_start, "subscription"))
            seen_keys.add((m_start, "consumed"))

        stale = self.sudo().search([("budget_id", "=", budget.id)])
        for row in stale:
            if (row.period_month, row.basis) not in seen_keys:
                row.unlink()

    @api.model
    def cron_rollup_all(self):
        budgets = self.env["ethara.project.budget"].sudo().search([("active", "=", True)])
        for budget in budgets:
            try:
                self._rollup_budget(budget)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "Ethara Project Budget monthly rollup failed for budget %s: %s",
                    budget.display_name,
                    exc,
                )
