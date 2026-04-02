# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class TalosDomain(models.Model):
    _name = 'talos.domain'
    _description = 'Talos Domain'
    _rec_name = 'name'

    name = fields.Char(string='Name')
    parent_id = fields.Many2one('talos.domain')
    child_ids = fields.One2many('talos.domain', 'parent_id', string='Children')