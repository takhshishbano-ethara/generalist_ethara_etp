from odoo import models, fields, api


class EtpAssessmentDimension(models.Model):
    _name = "etp.assessment.dimension"
    _description = "Assessment Dimension"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    option_ids = fields.One2many(
        "etp.assessment.dimension.option", "dimension_id", string="Options"
    )
    option_count = fields.Integer(
        string="Options", compute="_compute_option_count"
    )
    options_display = fields.Char(
        string="Options Preview", compute="_compute_options_display"
    )

    @api.depends("option_ids")
    def _compute_option_count(self):
        for rec in self:
            rec.option_count = len(rec.option_ids)

    @api.depends("option_ids", "option_ids.name")
    def _compute_options_display(self):
        for rec in self:
            parts = [opt.name for opt in rec.option_ids.sorted("sequence")]
            rec.options_display = ", ".join(parts) if parts else ""


class EtpAssessmentDimensionOption(models.Model):
    _name = "etp.assessment.dimension.option"
    _description = "Assessment Dimension Option"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    dimension_id = fields.Many2one(
        "etp.assessment.dimension", required=True, ondelete="cascade"
    )
