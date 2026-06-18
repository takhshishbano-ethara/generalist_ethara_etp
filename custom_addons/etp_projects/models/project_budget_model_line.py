from odoo import fields, models


class EtpProjectBudgetModelLine(models.Model):
    _name = "etp.project.budget.model.line"
    _description = "Project Budget Model Line"
    _order = "id"

    budget_id = fields.Many2one(
        "etp.project.aws.budget",
        string="Project Budget",
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
