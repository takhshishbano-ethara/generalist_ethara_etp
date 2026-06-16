from odoo import fields, models


class ClientDeliverableAiModel(models.Model):
    """Master list for the deliverable 'Model' dropdown (Opus, Sonnet, ...).

    Editable from the UI so new models can be added without code changes.
    """

    _name = "client.deliverable.ai.model"
    _description = "Deliverable AI Model"
    _order = "sequence, name"

    name = fields.Char(string="Model", required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "This model name already exists."),
    ]
