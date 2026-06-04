# -*- coding: utf-8 -*-
from odoo import fields, models


class VideoEditorSubCategory(models.Model):
    _name = "video.editor.sub.category"
    _description = "Crowley Sourcing Sub-Category"
    _order = "category_id, sequence, name"
    _rec_name = "name"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        help="Key written to video.editor.project.sub_category when this record is picked.",
    )
    category_id = fields.Many2one(
        "video.editor.category",
        string="Category",
        required=True,
        ondelete="restrict",
        index=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Sub-category code must be unique."),
    ]
