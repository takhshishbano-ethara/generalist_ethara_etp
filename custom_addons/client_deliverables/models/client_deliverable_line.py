from odoo import api, fields, models


class ClientDeliverableLine(models.Model):
    """One row per AI model on a deliverable.

    Each line carries its own task count and per-task cost; the deliverable's
    Total Tasks / Total Budget are summed from these lines.
    """

    _name = "client.deliverable.line"
    _description = "Client Deliverable Model Line"
    _order = "id"

    deliverable_id = fields.Many2one(
        "client.deliverable",
        string="Deliverable",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ai_model_id = fields.Many2one(
        "client.deliverable.ai.model", string="Model", required=True
    )
    per_task_cost = fields.Monetary(
        string="Per Task Cost", currency_field="currency_id"
    )
    line_total = fields.Monetary(
        string="Line Total",
        currency_field="currency_id",
        compute="_compute_line_total",
        store=True,
        help="Deliverable's Total Tasks × Per Task Cost.",
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="deliverable_id.currency_id",
        store=True,
        readonly=True,
    )

    @api.depends("deliverable_id.total_tasks", "per_task_cost")
    def _compute_line_total(self):
        for line in self:
            line.line_total = (
                line.deliverable_id.total_tasks or 0
            ) * (line.per_task_cost or 0.0)
