from odoo import fields, models


class EtpAiModel(models.Model):
    _name = "etp.ai.model"
    _description = "AI Model"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "This AI model name already exists."),
    ]
