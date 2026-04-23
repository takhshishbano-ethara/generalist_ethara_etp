# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError


class AtlasDomain(models.Model):
    _name = 'atlas.domain'
    _description = 'Atlas Domain'
    _rec_name = 'name'

    name = fields.Char(string='Name')
    parent_id = fields.Many2one('atlas.domain')
    child_ids = fields.One2many('atlas.domain', 'parent_id', string='Children')
    md_file1 = fields.Char(string='MD File 1')
    md_file2 = fields.Char(string='MD File 2')
    md_file3 = fields.Char(string='MD File 3')
