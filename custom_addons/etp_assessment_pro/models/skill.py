import logging

from odoo import models, fields, api
from odoo.exceptions import ValidationError

from ..constants import (
    QUESTION_TYPE_SELECTION,
    MEDIUM_SELECTION,
    DIFFICULTY_SELECTION,
)

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
        QUESTION_TYPE_SELECTION,
        required=True,
        default="mcq",
    )
    medium = fields.Selection(
        MEDIUM_SELECTION,
        default="text", required=True,
        help="The source medium this skill is about. Image question types "
             "(A/B, prompt/labelling) only make sense for an image medium; "
             "everything else is text.")
    question_count = fields.Integer(default=5, string="Questions to Generate")
    time_minutes = fields.Integer(default=10)
    # User-defined A/B axes for image_ab generation. Each selected dimension's
    # NAME is an axis and its OPTIONS are that axis's allowed verdicts. Empty =>
    # the generator falls back to the built-in rubric (Instruction Following /
    # Visual Quality / Less AI Generated / Overall Choice, verdicts Response
    # A|B|Both Good|Both Bad).
    image_ab_dimension_ids = fields.Many2many(
        "etp.assessment.pro.dimension",
        relation="etp_assessment_pro_skill_ab_dimension_rel",
        column1="skill_id", column2="dimension_id",
        string="Image A/B Dimensions",
        help="Axes the AI uses to score generated Image A/B questions. Each "
             "dimension's options are the allowed verdicts for that axis. "
             "Leave empty to use the default A/B rubric.")
    difficulty = fields.Selection(
        DIFFICULTY_SELECTION,
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

    # --- Generation status (per-skill, for batched + recoverable generation) ---
    gen_state = fields.Selection(
        [
            ("none", "Not generated"),
            ("queued", "Queued"),
            ("generating", "Generating"),
            ("done", "Generated"),
            ("failed", "Failed"),
        ],
        default="none", string="Generation State", copy=False,
        help="Per-skill generation status so a failed skill can be retried "
             "on its own without touching the others. Generation runs in the "
             "background (a cron drains 'queued' skills) so the slow LLM call "
             "never holds the web request / DB connection open.")
    gen_error = fields.Char(
        string="Generation Error", copy=False,
        help="Last generation failure reason for this skill, if any.")
    gen_prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", string="Generation Source Prompt",
        copy=False, ondelete="cascade",
        help="The prompt whose SOP questions are generated against. Set when "
             "generation is queued; the background cron generates for it.")
    gen_draft_count = fields.Integer(
        string="Draft Count", compute="_compute_gen_draft_count",
        help="How many draft questions currently exist for this skill.")

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "Skill names must be unique."),
    ]

    def _compute_bank_question_count(self):
        for rec in self:
            rec.bank_question_count = len(rec.question_ids)

    def _compute_gen_draft_count(self):
        Draft = self.env["etp.assessment.pro.prompt.question"].sudo()
        for rec in self:
            rec.gen_draft_count = Draft.search_count([
                ("skill_id", "=", rec.id), ("state", "=", "draft")])

    @api.constrains("medium", "question_type")
    def _check_medium_question_type(self):
        """Medium and question type must agree both ways: the image medium goes
        with image question types and ONLY those. Either mismatch makes the
        generator refuse, so block it at the source with a clear message."""
        image_types = ("image_ab", "image_text")
        for rec in self:
            is_image_type = rec.question_type in image_types
            is_image_medium = rec.medium == "image"
            if is_image_type and not is_image_medium:
                raise ValidationError(
                    "Skill %r uses an image question type (%s) but medium %r. "
                    "Image question types need the image medium." % (
                        rec.name, rec.question_type, rec.medium))
            if is_image_medium and not is_image_type:
                raise ValidationError(
                    "Skill %r uses the image medium with a non-image question "
                    "type (%s). The image medium is only for image question "
                    "types (A/B Evaluation, Prompt/Labelling) — those are the "
                    "only types that generate a picture. Use the text medium "
                    "for this question type." % (rec.name, rec.question_type))

    @api.onchange("question_type")
    def _onchange_question_type_medium(self):
        """Keep medium in step with the question type so the two can never drift
        into a blocked combination: image types force the image medium,
        everything else falls back to text."""
        if self.question_type in ("image_ab", "image_text"):
            self.medium = "image"
        elif self.medium == "image":
            self.medium = "text"

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
