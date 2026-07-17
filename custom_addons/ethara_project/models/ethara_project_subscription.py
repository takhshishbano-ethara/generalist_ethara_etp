from odoo import fields, models


class EtharaProjectSubscription(models.Model):
    _name = 'ethara.project.subscription'
    _description = 'Ethara Subscription Catalog'
    _order = 'sequence, name'

    name = fields.Char(string='Subscription Name', required=True)
    cost = fields.Float(string='Cost per Subscription (USD)')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'This subscription name already exists.'),
    ]
