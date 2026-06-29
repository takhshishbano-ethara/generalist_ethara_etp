from odoo import api, fields, models


class EtpBatchBudgetRequestSubscriptionLine(models.Model):
    _name = "etp.batch.budget.request.subscription.line"
    _description = "Phase Budget Request Subscription Line"
    _order = "id"

    request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Request",
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
        "etp_batch_budget_request_subscription_line_user_rel",
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
    requested_amount = fields.Float(string="Requested (USD)")
    approved_amount = fields.Float(string="Approved (USD)")
    per_day_cost = fields.Float(
        string="Per Day Cost (USD)",
        compute="_compute_per_day_cost",
        store=True,
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
