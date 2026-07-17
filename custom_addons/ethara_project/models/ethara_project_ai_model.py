from odoo import api, fields, models


class EtharaProjectAiModel(models.Model):
    _name = 'ethara.project.ai.model'
    _description = 'Ethara AI Model Provider'
    _order = 'sequence, name'

    name = fields.Char(string='Provider', required=True)
    provider = fields.Char(
        string='Provider Code',
        compute='_compute_provider',
        store=True,
        index=True,
    )
    api_url = fields.Char(string='API URL')
    api_key = fields.Char(string='API Key', groups='base.group_system')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'This AI model provider already exists.'),
    ]

    @api.depends('name')
    def _compute_provider(self):
        for rec in self:
            rec.provider = rec.name or ''
