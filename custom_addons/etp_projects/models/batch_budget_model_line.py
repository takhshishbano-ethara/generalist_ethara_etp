from odoo import fields, models


class EtpBatchBudgetModelLine(models.Model):
    _name = "etp.batch.budget.model.line"
    _description = "Batch Budget Model Line"
    _order = "id"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ai_model_id = fields.Many2one(
        "etp.ai.model",
        string="Model",
        required=True,
    )
    per_task_cost = fields.Float(string="Per Task Cost (USD)")
