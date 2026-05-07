# -*- coding: utf-8 -*-
from odoo import models, fields


class EtpAssessmentQuestion(models.Model):
    _name = "etp.assessment.question"
    _description = "Assessment Question"
    _order = "sequence, id"

    name = fields.Char(string="Title", required=True)
    sequence = fields.Integer(default=10)
    question_type = fields.Selection(
        [
            ("image_comparison", "Image Comparison"),
            ("text", "Text"),
            ("coding", "Coding"),
            ("image_text", "Image + Text"),
            ("video", "Video"),
        ],
        string="Question Type",
        required=True,
        default="text",
    )
    prompt = fields.Text(string="Prompt", required=True)
    description = fields.Text(string="Description")
    active = fields.Boolean(default=True)
    category_id = fields.Many2one(
        "etp.assessment.category", string="Category", ondelete="restrict"
    )
    question_dimension_ids = fields.One2many(
        "etp.assessment.question.dimension",
        "question_id",
        string="Dimensions",
    )

    image_a = fields.Binary(string="Response Image A", attachment=True)
    image_b = fields.Binary(string="Response Image B", attachment=True)
    image_a_url = fields.Char(string="Response A URL")
    image_b_url = fields.Char(string="Response B URL")

    code_snippet = fields.Text(string="Code Snippet")
    code_language = fields.Selection(
        [
            ("python", "Python"),
            ("javascript", "JavaScript"),
            ("java", "Java"),
            ("csharp", "C#"),
            ("cpp", "C++"),
            ("go", "Go"),
            ("rust", "Rust"),
            ("other", "Other"),
        ],
        string="Language",
        default="python",
    )

    video_url = fields.Char(string="Video URL")
