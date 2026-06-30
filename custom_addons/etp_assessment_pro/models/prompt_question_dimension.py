# -*- coding: utf-8 -*-
"""Editable answer key for a DRAFT question — one axis per dimension, seeded from the LLM JSON."""

from odoo import models, fields


class EtpAssessmentPromptQuestionDimension(models.Model):
    _name = "etp.assessment.pro.prompt.question.dimension"
    _description = "Draft Question Answer Axis"
    _order = "sequence, id"

    draft_id = fields.Many2one(
        "etp.assessment.pro.prompt.question",
        string="Draft Question", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    label = fields.Char(
        string="Axis", required=True,
        help="For MCQ/MSQ this is the question's answer set; for image A/B it is "
             "one scoring axis (e.g. Instruction Following).")
    option_line_ids = fields.One2many(
        "etp.assessment.pro.prompt.question.dimension.option",
        "dimension_id", string="Options")


class EtpAssessmentPromptQuestionDimensionOption(models.Model):
    _name = "etp.assessment.pro.prompt.question.dimension.option"
    _description = "Draft Question Answer Option"
    _order = "sequence, id"

    dimension_id = fields.Many2one(
        "etp.assessment.pro.prompt.question.dimension",
        string="Axis", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Option", required=True)
    is_correct = fields.Boolean(string="Correct", default=False)
