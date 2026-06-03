# -*- coding: utf-8 -*-
from odoo import fields, models


class VideoEditorCategory(models.Model):
    _name = "video.editor.category"
    _description = "Crowley Sourcing Category"
    _order = "sequence, name"
    _rec_name = "name"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help="Key matching the video.editor.project.category Selection value.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [("code_uniq", "unique(code)", "Category code must be unique.")]
