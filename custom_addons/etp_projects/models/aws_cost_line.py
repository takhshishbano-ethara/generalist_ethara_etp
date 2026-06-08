from odoo import api, fields, models


class EtpProjectAwsCostLine(models.Model):
    _name = "etp.project.aws.cost.line"
    _description = "Project AWS Cost Line"
    _order = "period desc, amount_source desc"

    budget_id = fields.Many2one(
        "etp.project.aws.budget", required=True, ondelete="cascade", index=True,
    )
    project_id = fields.Many2one(
        related="budget_id.project_id", store=True, readonly=True, index=True,
    )
    tag_key = fields.Char(related="budget_id.tag_key", store=True, readonly=True)
    tag_value = fields.Char(related="budget_id.tag_value", store=True, readonly=True)

    period = fields.Date(required=True, index=True)
    period_label = fields.Char(compute="_compute_period_label", store=True)
    service_name = fields.Char(required=True, index=True)

    currency_id = fields.Many2one(
        "res.currency",
        default=lambda s: s.env.ref("base.USD", raise_if_not_found=False)
        or s.env.company.currency_id,
    )
    amount_source = fields.Monetary(currency_field="currency_id", string="Cost (USD)")
    inr_currency_id = fields.Many2one(
        "res.currency",
        default=lambda s: s.env.ref("base.INR", raise_if_not_found=False)
        or s.env.company.currency_id,
    )
    amount_inr = fields.Monetary(
        currency_field="inr_currency_id", compute="_compute_inr", store=True,
        string="Cost (INR)",
    )

    _uniq_line = models.Constraint(
        "unique(budget_id, period, service_name)",
        "One row per budget/period/service.",
    )

    @api.depends("period")
    def _compute_period_label(self):
        for rec in self:
            rec.period_label = rec.period.strftime("%Y-%m") if rec.period else ""

    @api.depends("amount_source", "currency_id", "period")
    def _compute_inr(self):
        inr = self.env.ref("base.INR", raise_if_not_found=False)
        for rec in self:
            if inr and rec.currency_id and rec.currency_id.id != inr.id and rec.amount_source:
                rec.amount_inr = rec.currency_id._convert(
                    rec.amount_source, inr, self.env.company,
                    rec.period or fields.Date.today(),
                )
            else:
                rec.amount_inr = rec.amount_source
