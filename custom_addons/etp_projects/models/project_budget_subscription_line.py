from odoo import api, fields, models


class EtpProjectBudgetSubscriptionLine(models.Model):
    _name = "etp.project.budget.subscription.line"
    _description = "Project Budget Subscription Line"
    _order = "id"

    budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    subscription_id = fields.Many2one(
        "etp.subscription",
        string="Subscription",
        required=True,
        ondelete="restrict",
    )
    name = fields.Char(
        string="Subscription Name",
        related="subscription_id.name",
        store=True,
        readonly=True,
    )
    cost_per_subscription = fields.Float(
        string="Cost per Subscription (USD)",
        related="subscription_id.cost",
        store=True,
        readonly=True,
    )
    assigned_user_ids = fields.Many2many(
        "res.users",
        "etp_project_budget_subscription_line_user_rel",
        "subscription_line_id",
        "user_id",
        string="Assigned To",
    )
    subscription_count = fields.Integer(
        string="No. of Subscriptions",
        compute="_compute_subscription_count",
        store=True,
    )
    final_amount = fields.Float(
        string="Monthly Cost (USD)",
        compute="_compute_final_amount",
        store=True,
    )
    approved_amount = fields.Float(string="Approved (USD)")
    per_day_cost = fields.Float(
        string="Per Day Cost (USD)",
        compute="_compute_per_day_cost",
        store=True,
    )
    start_date = fields.Date(
        string="Start Date",
        help="Earliest start date across all approved subscription windows.",
    )
    end_date = fields.Date(
        string="End Date",
        help="Latest expiry date across all approved subscription windows "
             "(extends by 30 days on each re-approval).",
    )

    @api.depends("assigned_user_ids")
    def _compute_subscription_count(self):
        for rec in self:
            rec.subscription_count = len(rec.assigned_user_ids)

    @api.depends("cost_per_subscription", "subscription_count")
    def _compute_final_amount(self):
        for rec in self:
            rec.final_amount = (
                (rec.cost_per_subscription or 0.0)
                * (rec.subscription_count or 0)
            )

    @api.depends("final_amount")
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.final_amount or 0.0) / 30.0
