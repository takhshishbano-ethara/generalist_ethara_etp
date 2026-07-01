from odoo import api, fields, models


class EtpBatchBudgetInfraLine(models.Model):
    _name = "etp.batch.budget.infra.line"
    _description = "Phase Budget Infrastructure Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
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
    budget_amount = fields.Float(string="Budget (USD)")
    approved_amount = fields.Float(
        string="Approved (USD)",
        help="Sum of approved amounts from request lines targeting this infrastructure.",
    )
    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")
    per_day_cost = fields.Float(
        string="Per Day Cost (USD)",
        compute="_compute_per_day_cost",
        store=True,
    )
    consumed_amount = fields.Float(
        string="Consumed (USD)",
        compute="_compute_consumed_amount",
        help="Infrastructure cost incurred within the line date range.",
    )
    remaining_amount = fields.Float(
        string="Remaining (USD)",
        compute="_compute_consumed_amount",
    )

    @api.depends("budget_amount")
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.budget_amount or 0.0) / 30.0

    @api.depends(
        "approved_amount",
        "infra_type_id",
        "start_date",
        "end_date",
        "batch_id.project_id",
    )
    def _compute_consumed_amount(self):
        Line = self.env["etp.project.aws.cost.line"]
        for rec in self:
            project = rec.batch_id.project_id if rec.batch_id else False
            consumed = 0.0
            if (
                project
                and rec.start_date
                and rec.end_date
                and rec.end_date >= rec.start_date
                and rec.infra_type_id
                and rec.infra_type_id.name
            ):
                consumed = sum(
                    Line.sudo().search([
                        ("project_id", "=", project.id),
                        ("granularity", "=", "day"),
                        ("period", ">=", rec.start_date),
                        ("period", "<=", rec.end_date),
                        ("service_name", "=", rec.infra_type_id.name),
                        ("source_tag_key", "=", "project"),
                    ]).mapped("amount_source")
                )
            rec.consumed_amount = consumed
            rec.remaining_amount = (rec.approved_amount or 0.0) - consumed
