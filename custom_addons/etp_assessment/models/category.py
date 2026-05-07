# -*- coding: utf-8 -*-
from odoo import models, fields


class EtpAssessmentCategory(models.Model):
    _name = "etp.assessment.category"
    _description = "Assessment Question Category"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text()
    question_ids = fields.One2many(
        "etp.assessment.question", "category_id", string="Questions"
    )
    question_count = fields.Integer(
        compute="_compute_question_count", string="# Questions"
    )

    def _compute_question_count(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)
