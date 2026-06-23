import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


class EtpAssessmentSkill(models.Model):
    _name = "etp.assessment.pro.skill"
    _description = "Assessment Skill"
    _order = "sequence, name"

    name = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    description = fields.Text()
    tags = fields.Char(help="Comma-separated")
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
            ("image_ab", "Image - A/B Evaluation"),
            ("image_text", "Image - Prompt/Labelling"),
        ],
        required=True,
        default="mcq",
    )
    question_count = fields.Integer(default=5, string="Questions to Generate")
    time_minutes = fields.Integer(default=10)
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        default="medium",
    )
    source_resource_ids = fields.Many2many("etp.assessment.pro.prompt.resource")
    extracted_from_prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", ondelete="set null"
    )
    question_ids = fields.Many2many(
        "etp.assessment.pro.question",
        relation="etp_assessment_pro_question_skill_rel",
        column1="skill_id",
        column2="question_id",
        string="Bank Questions",
    )
    bank_question_count = fields.Integer(compute="_compute_bank_question_count")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "Skill names must be unique."),
    ]

    def _compute_bank_question_count(self):
        for rec in self:
            rec.bank_question_count = len(rec.question_ids)

    def action_generate_questions(self):
        """Generate question drafts for one/many skills via their source generator."""
        with_generator = self.filtered(lambda s: s.extracted_from_prompt_id)
        skipped = self - with_generator

        groups = {}
        for skill in with_generator:
            groups.setdefault(skill.extracted_from_prompt_id, self.browse())
            groups[skill.extracted_from_prompt_id] |= skill

        generated_skills = 0
        generated_groups = 0
        failures = []
        for prompt, group_skills in groups.items():
            previous = prompt.selected_skill_ids
            try:
                prompt.selected_skill_ids = [(6, 0, group_skills.ids)]
                prompt.action_generate_questions()
                generated_skills += len(group_skills)
                generated_groups += 1
            except Exception as exc:  # noqa: BLE001 - one group must not abort rest
                _logger.exception(
                    "Skill-bank generation failed for prompt %s (%s skills)",
                    prompt.id, len(group_skills),
                )
                failures.append("%s: %s" % (prompt.display_name, exc))
            finally:
                prompt.selected_skill_ids = [(6, 0, previous.ids)]

        message_parts = [
            "Generated questions for %s skill(s) across %s generator(s)." % (
                generated_skills, generated_groups,
            )
        ]
        if skipped:
            message_parts.append(
                "Skipped %s skill(s) with no source generator: %s." % (
                    len(skipped), ", ".join(skipped.mapped("name")),
                )
            )
        if failures:
            message_parts.append(
                "%s generator(s) failed: %s" % (len(failures), "; ".join(failures))
            )

        if failures:
            notif_type = "danger"
        elif skipped:
            notif_type = "warning"
        else:
            notif_type = "success"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Generate Questions",
                "message": " ".join(message_parts),
                "type": notif_type,
                "sticky": bool(skipped or failures),
            },
        }
