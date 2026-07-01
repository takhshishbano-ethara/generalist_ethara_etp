from odoo import api, fields, models


class EtpBatchBudgetSubscriptionLine(models.Model):
    _name = "etp.batch.budget.subscription.line"
    _description = "Phase Budget Subscription Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
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
        "etp_batch_budget_subscription_line_user_rel",
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
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    consumed_amount = fields.Float(
        string="Consumed (USD)",
        compute="_compute_consumed_amount",
        help="Per-day cost x days elapsed within the subscription date range "
             "(capped at today).",
    )
    remaining_amount = fields.Float(
        string="Remaining (USD)",
        compute="_compute_consumed_amount",
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

    @api.depends(
        "approved_amount",
        "per_day_cost",
        "start_date",
        "end_date",
        "batch_id.start_date",
        "batch_id.end_date",
    )
    def _compute_consumed_amount(self):
        today = fields.Date.context_today(self)
        for rec in self:
            batch = rec.batch_id
            start_date = rec.start_date or (batch.start_date if batch else False)
            end_date = rec.end_date or (batch.end_date if batch else False)
            consumed = 0.0
            if (
                start_date
                and end_date
                and end_date >= start_date
                and (rec.per_day_cost or 0.0) > 0.0
            ):
                upper = min(today, end_date)
                if upper >= start_date:
                    days = (upper - start_date).days + 1
                    consumed = days * (rec.per_day_cost or 0.0)
            rec.consumed_amount = consumed
            rec.remaining_amount = (rec.approved_amount or 0.0) - consumed
