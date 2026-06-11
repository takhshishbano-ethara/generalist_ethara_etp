# -*- coding: utf-8 -*-
"""Extension of etp.assessment.question with the design-spec fields.

ASSESSMENT-WORKFLOW.md §0 defines three task types (Eval / Prompt / BBox); §12.2
adds difficulty, day-number, the locked correct/wrong answer pair, and the
flagged_bad gate that SCR-097 sets via the Distribution drawer.
"""
import json

from odoo import api, fields, models


class EtpAssessmentQuestion(models.Model):
    _inherit = "etp.assessment.question"

    code = fields.Char(
        string="Question Code",
        help="Mono id shown on every screen (QST-#####).",
        copy=False,
        index=True,
    )
    task_type = fields.Selection(
        [
            ("eval_compare", "Eval Compare"),
            ("prompt_writing", "Prompt Writing"),
            ("bbox_labeling", "BBox Labeling"),
        ],
        string="Task type",
        help="WORKFLOW §0. Drives the per-type pill on every screen + the §6.2 scoring scheme.",
    )
    difficulty = fields.Selection(
        [
            ("easy", "Easy"),
            ("mixed", "Mixed"),
            ("hard", "Hard"),
        ],
        default="mixed",
        string="Difficulty",
    )
    day_number = fields.Integer(
        string="Day",
        help="The 1-based day this question belongs to inside the assessment window.",
    )
    correct_answer = fields.Text(
        string="Locked Answer Key (JSON)",
        help="The immutable correct answer payload — §0/§6.1. JSON-encoded.",
    )
    wrong_answer = fields.Text(
        string="Locked Distractor (JSON)",
        help="The locked wrong-answer pair used for context — §0. JSON-encoded.",
    )
    flagged_bad = fields.Boolean(
        string="Flagged for regeneration",
        default=False,
        help="SCR-097 → Distribution drawer → Flag for regeneration. WORKFLOW §7.2.",
    )

    submission_ids = fields.One2many(
        "etp.assessment.submission",
        "question_id",
        string="Submissions",
    )

    response_count = fields.Integer(
        compute="_compute_response_stats", string="Responses"
    )
    avg_score = fields.Float(
        compute="_compute_response_stats", string="Avg score", digits=(5, 2)
    )
    low_confidence_pct = fields.Float(
        compute="_compute_response_stats", string="% low-confidence", digits=(5, 2)
    )
    is_suspect = fields.Boolean(
        compute="_compute_response_stats",
        string="Flagged-suspect (auto)",
        help="Auto-flag per SCR-097 design notes: avg<25 OR (avg>95 + collapsed variance) OR low_conf% > 40.",
    )

    @api.depends(
        "submission_ids",
        "submission_ids.final_score",
        "submission_ids.low_confidence",
    )
    def _compute_response_stats(self):
        for rec in self:
            graded = rec.submission_ids.filtered(
                lambda s: s.state in ("scored", "overridden") and s.final_score is not False
            )
            count = len(graded)
            if count:
                avg = sum(s.final_score or 0 for s in graded) / count
                low = sum(1 for s in graded if s.low_confidence)
                low_pct = (low / count) * 100.0
            else:
                avg = 0.0
                low_pct = 0.0
            rec.response_count = count
            rec.avg_score = avg
            rec.low_confidence_pct = low_pct

            suspect = False
            if count >= 3:
                if avg < 25:
                    suspect = True
                elif avg > 95:
                    scores = [s.final_score or 0 for s in graded]
                    if scores and (max(scores) - min(scores) <= 10):
                        suspect = True
                if low_pct > 40:
                    suspect = True
            rec.is_suspect = suspect

    def parsed_correct_answer(self):
        self.ensure_one()
        if not self.correct_answer:
            return None
        try:
            return json.loads(self.correct_answer)
        except (TypeError, ValueError):
            return self.correct_answer

    def parsed_wrong_answer(self):
        self.ensure_one()
        if not self.wrong_answer:
            return None
        try:
            return json.loads(self.wrong_answer)
        except (TypeError, ValueError):
            return self.wrong_answer

    def action_flag_for_regeneration(self):
        """Set Question.flagged_bad = True without unlocking. WORKFLOW §7.2."""
        for rec in self:
            rec.flagged_bad = True
        return True
