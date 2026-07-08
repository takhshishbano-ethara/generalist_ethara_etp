from odoo import api, fields, models


class EtpBatchBudgetRequestInfraLine(models.Model):
    _name = "etp.batch.budget.request.infra.line"
    _description = "Phase Budget Request Infrastructure Line"
    _order = "id"

    request_id = fields.Many2one(
        "etp.batch.budget.request",
        string="Request",
        required=True,
        ondelete="cascade",
        index=True,
    )
    infra_type_id = fields.Many2one(
        "etp.infra.type",
        string="Infrastructure",
        required=True,
    )
    description = fields.Char(string="Description")
    requested_amount = fields.Float(string="Requested (USD)")
    approved_amount = fields.Float(string="Approved (USD)")
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    per_day_requested = fields.Float(
        string="Per Day Requested (USD)",
        compute="_compute_per_day_amounts",
        store=True,
    )
    per_day_approved = fields.Float(
        string="Per Day Approved (USD)",
        compute="_compute_per_day_amounts",
        store=True,
    )

    aws_sku_id = fields.Many2one(
        "etp.aws.pricing.sku",
        string="AWS SKU",
        ondelete="set null",
        help="Optional link to a specific priced AWS SKU snapshot.",
    )
    unit_price_usd = fields.Float(
        string="Unit Price (USD)",
        digits=(16, 6),
        help="Snapshot of AWS unit price at time of budget creation.",
    )
    price_unit = fields.Char(string="Price Unit")
    quantity = fields.Float(string="Quantity", default=1.0)
    duration_hours = fields.Float(string="Duration (Hours)", default=720.0)
    computed_amount = fields.Float(
        string="Computed Amount (USD)",
        compute="_compute_amount",
        store=True,
        digits=(16, 2),
    )

    @api.depends("requested_amount", "approved_amount")
    def _compute_per_day_amounts(self):
        for rec in self:
            rec.per_day_requested = (rec.requested_amount or 0.0) / 30.0
            rec.per_day_approved = (rec.approved_amount or 0.0) / 30.0

    @api.depends("unit_price_usd", "quantity", "duration_hours")
    def _compute_amount(self):
        for rec in self:
            rec.computed_amount = (
                (rec.unit_price_usd or 0.0)
                * (rec.quantity or 0.0)
                * (rec.duration_hours or 0.0)
            )

    @api.depends("unit_price_usd", "quantity", "duration_hours")
    def _compute_amount(self):
        for rec in self:
            rec.computed_amount = (
                (rec.unit_price_usd or 0.0)
                * (rec.quantity or 0.0)
                * (rec.duration_hours or 0.0)
            )
