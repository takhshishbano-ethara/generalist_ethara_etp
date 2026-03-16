# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class DomainLevel(models.Model):
    _name = 'domain.level'
    _description = 'Domain Level'
    _rec_name = 'name'

    name = fields.Char(string='Name')
    parent_id = fields.Many2one('domain.level')
    child_ids = fields.One2many('domain.level', 'parent_id', string='Children')