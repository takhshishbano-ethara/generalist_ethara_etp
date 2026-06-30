import calendar

from odoo import api, fields, models


class EtpProjectBudgetMonthlyCost(models.Model):
    _name = "etp.project.budget.monthly.cost"
    _description = "Project Budget Monthly Cost Rollup"
    _order = "budget_id, period_month desc, basis"

    budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
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
        [("infra", "Infrastructure"), ("subscription", "Subscription")],
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
            rec.month_label = rec.period_month.strftime("%B %Y") if rec.period_month else ""

    @api.depends("monthly_cost", "period_month")
    def _compute_per_day_avg_cost(self):
        for rec in self:
            if rec.period_month:
                days = calendar.monthrange(rec.period_month.year, rec.period_month.month)[1]
                rec.per_day_avg_cost = (rec.monthly_cost or 0.0) / days
            else:
                rec.per_day_avg_cost = 0.0
