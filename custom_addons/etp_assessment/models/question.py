# -*- coding: utf-8 -*-
from odoo import models, fields, api


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

    # ------------------------------------------------------------------
    # Imported / generated answer-key + rubric (from the research-team
    # question-bank JSON, output-schema.json). Objective field answers are
    # stored as is_correct flags on dimension options; subjective field
    # rubrics (checklist / constraints / pass_condition) are stored here as
    # JSON and fed to the LLM scorer.
    # ------------------------------------------------------------------
    grading_json = fields.Text(
        string="Grading (raw)",
        help="Verbatim grading object from the imported question bank "
             "(per-field objective answer or subjective rubric). For PL "
             "spot-check + audit.")
    subjective_rubric_json = fields.Text(
        string="Subjective Rubric (JSON)",
        help="List of subjective field rubrics: "
             "[{key,label,checklist[],constraints[],pass_condition}]. "
             "Fed to the LLM when grading this question's justification.")
    meta_json = fields.Text(
        string="Meta (JSON)",
        help="scenario_type / answer_pattern / difficulty / trap from "
             "the imported bank.")
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        string="Difficulty")
    has_subjective = fields.Boolean(
        string="Has Subjective Field", compute="_compute_has_subjective",
        store=True)
    source_ref = fields.Char(
        string="Source Ref",
        help="Origin of this question: 'json:<project>#<id>' on import, "
             "'gen:<prompt>' on LLM generation.")

    @api.depends("subjective_rubric_json")
    def _compute_has_subjective(self):
        for rec in self:
            rec.has_subjective = bool(
                (rec.subjective_rubric_json or "").strip()
                and rec.subjective_rubric_json.strip() not in ("[]", "{}"))
