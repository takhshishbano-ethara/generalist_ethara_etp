from odoo import api, fields, models


class EtharaProjectInfraProvider(models.Model):
    """A cloud infrastructure provider (AWS, Microsoft, ...).

    Mirrors ``ethara.project.ai.model`` (the AI model provider catalog): admins
    add / edit / archive rows from the Odoo backend and the mobile app only
    reads the active ones. ``pricing_source`` tells the client how to price the
    provider's services — ``aws_live`` uses the live AWS pricing configurator,
    ``manual`` uses a plain unit-price form.
    """

    _name = 'ethara.project.infra.provider'
    _description = 'Ethara Infrastructure Provider'
    _order = 'sequence, name'

    name = fields.Char(string='Provider', required=True)
    code = fields.Char(
        string='Provider Code',
        compute='_compute_code',
        store=True,
        index=True,
    )
    pricing_source = fields.Selection(
        selection=[
            ('aws_live', 'AWS Live Pricing'),
            ('manual', 'Manual Entry'),
        ],
        string='Pricing Source',
        default='manual',
        required=True,
        help='How the client prices this provider\'s services: '
             'aws_live uses the live AWS pricing picker, '
             'manual uses a plain unit-price form.',
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'This infrastructure provider already exists.'),
    ]

    @api.depends('name')
    def _compute_code(self):
        for rec in self:
            rec.code = (rec.name or '').strip().lower()
