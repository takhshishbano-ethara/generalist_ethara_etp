from odoo import fields, models


class EtpBatchBudgetTopupReason(models.Model):
    _name = "etp.batch.budget.topup.reason"
    _description = "Phase Budget Top-up Reason"
    _order = "sequence, name"

    name = fields.Char(string="Reason", required=True)
    code = fields.Char(string="Code")
    description = fields.Text(string="Description")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "This top-up reason already exists."),
    ]
