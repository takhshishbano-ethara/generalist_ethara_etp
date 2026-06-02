# -*- coding: utf-8 -*-
from odoo import fields, models


def _category_selection(self):
    field = self.env["video.editor.project"]._fields.get("category")
    return field.selection if field else []


class VideoEditorSubCategory(models.Model):
    _name = "video.editor.sub.category"
    _description = "Crowley Sourcing Sub-Category"
    _order = "category, sequence, name"
    _rec_name = "name"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        help="Key written to video.editor.project.sub_category when this record is picked.",
    )
    category = fields.Selection(
        selection=_category_selection,
        required=True,
        index=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Sub-category code must be unique."),
    ]
