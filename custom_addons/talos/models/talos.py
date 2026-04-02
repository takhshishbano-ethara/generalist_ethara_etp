from odoo import models, fields, api


class Talos(models.Model):
    _name = 'talos.talos'
    _description = 'Talos'

    name = fields.Char(string='Name', required=True)
    description = fields.Text(string='Description')
    active = fields.Boolean(default=True)
