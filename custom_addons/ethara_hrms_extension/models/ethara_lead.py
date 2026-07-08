from odoo import api, fields, models


class EtharaLead(models.Model):
    _name = 'ethara.lead'
    _description = 'Ethara Lead'
    _order = 'create_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(string='Name', compute='_compute_name', store=True)
    first_name = fields.Char(string='First Name', required=True)
    last_name = fields.Char(string='Last Name', required=True)
    email = fields.Char(string='Email', required=True, index=True)
    company = fields.Char(string='Company')
    query_type = fields.Char(string='Query Type')
    message = fields.Text(string='Message')

    @api.depends('first_name', 'last_name')
    def _compute_name(self):
        for rec in self:
            rec.name = ' '.join(p for p in [rec.first_name, rec.last_name] if p) or ''
