from odoo import fields, models


class EtpInfraType(models.Model):
    _name = "etp.infra.type"
    _description = "Infrastructure Type"
    _order = "sequence, name"

    name = fields.Char(string="Name", required=True)
    code = fields.Char(string="Code")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "This infrastructure type already exists."),
    ]
