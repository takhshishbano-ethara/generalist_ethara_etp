from odoo import fields, models


class EtharaProjectPhaseTopupReason(models.Model):
    _name = 'ethara.project.phase.topup.reason'
    _description = 'Ethara Project Phase Top-up Reason'
    _order = 'sequence, name'

    name = fields.Char(string='Reason', required=True)
    description = fields.Text(string='Description')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'This top-up reason already exists.'),
    ]
