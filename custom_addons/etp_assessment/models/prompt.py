# -*- coding: utf-8 -*-
import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EtpAssessmentPrompt(models.Model):
    """One LLM question-generation session: paste text -> skills -> questions."""
    _name = "etp.assessment.prompt"
    _description = "Assessment Prompt (LLM Question Generator)"
    _order = "create_date desc"

    name = fields.Char(string="Title", default="New Prompt", required=True)
    source_text = fields.Text(
        string="Source Text (SOP / instructions)",
        help="Paste the project SOP, instructions, or any reference text.",
    )
    generation_mode = fields.Selection(
        [
            ("seed", "Seed (SOP + golden example, 1 call)"),
            ("skills", "Skills (extract skills first, 2 calls)"),
        ],
        default="seed",
        required=True,
        string="Generation Mode",
        help="Seed: research-team flow — one LLM call takes the SOP plus one "
             "golden example question and generates the full set directly. "
             "Skills: legacy two-stage flow (extract skills, then generate "
             "per skill).",
    )
    golden_example = fields.Text(
        string="Golden Example Question",
        help="One exemplary question (full text) demonstrating the desired "
             "style, depth and format. Required for Seed mode.",
    )
    max_questions = fields.Integer(
        string="Max Questions",
        default=0,
        help="Seed mode: how many questions to generate. 0 = let the model decide.",
    )
    category_id = fields.Many2one(
        "etp.assessment.category",
        string="Target Category",
        help="Approved questions are added to this category in the Question Bank.",
    )
    skill_ids = fields.One2many(
        "etp.assessment.prompt.skill", "prompt_id", string="Skills"
    )
    question_ids = fields.One2many(
        "etp.assessment.prompt.question", "prompt_id", string="Generated Questions"
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("skills_ready", "Skills Extracted"),
            ("generating", "Generating"),
            ("done", "Done"),
        ],
        default="draft",
        string="State",
    )
    question_count = fields.Integer(compute="_compute_counts")
    approved_count = fields.Integer(compute="_compute_counts")

    @api.depends("question_ids", "question_ids.state")
    def _compute_counts(self):
        for rec in self:
            rec.question_count = len(rec.question_ids)
            rec.approved_count = len(
                rec.question_ids.filtered(lambda q: q.state == "approved")
            )

    # ---- serialization helpers (used by the JSON API for Flutter) ----
    def _skill_dict(self, skill):
        return {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description or "",
            "max_questions": skill.max_questions,
            "generated": skill.generated,
        }

    def _question_dict(self, q):
        return {
            "id": q.id,
            "skill": q.skill or "",
            "name": q.name,
            "question_prompt": q.question_prompt or "",
            "question_type": q.question_type or "text",
            "state": q.state,
            "approved_question_id": q.approved_question_id.id or False,
            "image_prompt_a": q.image_prompt_a or "",
            "image_prompt_b": q.image_prompt_b or "",
            "image_state": q.image_state or "none",
            "image_error": q.image_error or "",
        }

    def to_dict(self, include_children=True):
        """Full prompt payload for the mobile app to (re)hydrate its screen."""
        self.ensure_one()
        data = {
            "id": self.id,
            "name": self.name,
            "source_text": self.source_text or "",
            "generation_mode": self.generation_mode or "seed",
            "golden_example": self.golden_example or "",
            "max_questions": self.max_questions or 0,
            "category_id": self.category_id.id or False,
            "category_name": self.category_id.name or "",
            "state": self.state,
            "question_count": self.question_count,
            "approved_count": self.approved_count,
            "create_date": self.create_date.isoformat() if self.create_date else False,
        }
        if include_children:
            data["skills"] = [self._skill_dict(s) for s in self.skill_ids]
            data["questions"] = [self._question_dict(q) for q in self.question_ids]
        return data

    # ---- target category: self-managing ----
    def _get_or_create_category(self):
        """Return the prompt's target category, auto-creating one if unset.

        Without a category, approved questions would be invisible to every
        assessment (action_start composes purely by category). One SOP ->
        one auto category keeps the chain intact with zero admin effort.
        """
        self.ensure_one()
        if self.category_id:
            return self.category_id
        Category = self.env["etp.assessment.category"]
        name = "Gen: %s" % (self.name or "Prompt %s" % self.id)
        category = Category.search([("name", "=", name)], limit=1)
        if not category:
            category = Category.create({"name": name})
        self.category_id = category.id
        return category

    # ---- editable system prompts (Leviathan pattern) ----
    @api.model
    def _get_seed_system_prompt(self):
        from ..services.bedrock_questions import DEFAULT_SEED_PROMPT
        return self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.seed_system_prompt", "") or DEFAULT_SEED_PROMPT

    @api.model
    def _get_skills_system_prompt(self):
        from ..services.bedrock_questions import DEFAULT_SKILLS_PROMPT
        return self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.skills_system_prompt", "") or DEFAULT_SKILLS_PROMPT

    @api.model
    def _get_questions_system_prompt(self):
        from ..services.bedrock_questions import DEFAULT_QUESTIONS_PROMPT
        return self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.questions_system_prompt", "") or DEFAULT_QUESTIONS_PROMPT

    # ---- RPC entry points used by the Prompt UI ----
    def action_extract_skills(self):
        """Call the LLM, replace skills, return them for progressive render."""
        from ..services import bedrock_questions
        self.ensure_one()
        self.skill_ids.unlink()
        skills = bedrock_questions.extract_skills(
            self.env, self.source_text or "", self._get_skills_system_prompt()
        )
        seq = 10
        created = []
        for s in skills:
            rec = self.env["etp.assessment.prompt.skill"].create({
                "prompt_id": self.id,
                "name": s["skill"],
                "description": s["reason"],
                "max_questions": s["suggested_max"],
                "sequence": seq,
            })
            created.append({
                "id": rec.id, "name": rec.name,
                "description": rec.description, "max_questions": rec.max_questions,
            })
            seq += 10
        self.state = "skills_ready"
        return created

    def action_generate_questions(self):
        """Generate draft questions. ONE Bedrock/LLM text call either way.

        seed mode (default, research-team flow): SOP + golden example ->
        questions directly, no skills stage.
        skills mode (legacy): uses previously extracted skills.
        """
        from ..services import bedrock_questions
        self.ensure_one()
        # clear previous drafts (keep approved/denied history? -> drop drafts only)
        self.question_ids.filtered(lambda q: q.state == "draft").unlink()

        if self.generation_mode == "seed":
            if not (self.golden_example or "").strip():
                raise UserError(
                    "Seed mode needs a Golden Example Question — paste one "
                    "exemplary question that shows the desired style and depth."
                )
            qs = bedrock_questions.generate_questions_from_seed(
                self.env,
                self.source_text or "",
                self.golden_example,
                self._get_seed_system_prompt(),
                max_questions=self.max_questions or 0,
            )
        else:
            skills = [
                {"name": s.name, "max_questions": s.max_questions}
                for s in self.skill_ids
            ]
            qs = bedrock_questions.generate_questions(
                self.env, self.source_text or "", skills,
                self._get_questions_system_prompt(),
            )
        created = []
        for q in qs:
            is_imgcmp = q["type"] == "image_comparison"
            rec = self.env["etp.assessment.prompt.question"].create({
                "prompt_id": self.id,
                "skill": q["skill"],
                "name": q["title"],
                "question_prompt": q["prompt"],
                "question_type": q["type"],
                "image_prompt_a": q.get("image_prompt_a") or False,
                "image_prompt_b": q.get("image_prompt_b") or False,
                "image_state": "pending" if is_imgcmp else "none",
            })
            created.append({
                "id": rec.id, "skill": rec.skill, "name": rec.name,
                "question_prompt": rec.question_prompt,
                "question_type": rec.question_type, "state": rec.state,
                "image_prompt_a": rec.image_prompt_a or "",
                "image_prompt_b": rec.image_prompt_b or "",
                "image_state": rec.image_state,
            })
        self.state = "done"
        return created


class EtpAssessmentPromptSkill(models.Model):
    """A skill the LLM extracted from the source text."""
    _name = "etp.assessment.prompt.skill"
    _description = "Prompt Skill"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    name = fields.Char(string="Skill", required=True)
    description = fields.Text(string="Why relevant")
    sequence = fields.Integer(default=10)
    max_questions = fields.Integer(string="Max Questions", default=5)
    generated = fields.Boolean(default=False)


class EtpAssessmentPromptQuestion(models.Model):
    """A draft question generated by the LLM, pending approve/deny."""
    _name = "etp.assessment.prompt.question"
    _description = "Prompt Draft Question"
    _order = "id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    skill = fields.Char(string="Skill")
    name = fields.Char(string="Title", required=True)
    question_prompt = fields.Text(string="Question Prompt")
    question_type = fields.Selection(
        [
            ("text", "Text"),
            ("coding", "Coding"),
            ("image_comparison", "Image Comparison"),
            ("image_text", "Image + Text"),
            ("video", "Video"),
        ],
        default="text",
    )
    # --- image_comparison support: LLM emits two text-to-image prompts,
    # bedrock_images turns them into actual images before/at approval ---
    image_prompt_a = fields.Text(
        string="Image Prompt A",
        help="Text-to-image prompt for Response A (image_comparison only).",
    )
    image_prompt_b = fields.Text(
        string="Image Prompt B",
        help="Text-to-image prompt for Response B (image_comparison only).",
    )
    image_a = fields.Binary(string="Generated Image A", attachment=True)
    image_b = fields.Binary(string="Generated Image B", attachment=True)
    image_state = fields.Selection(
        [
            ("none", "Not Needed"),
            ("pending", "Pending Generation"),
            ("generated", "Generated"),
            ("failed", "Failed"),
        ],
        default="none",
        string="Images",
    )
    image_error = fields.Char(string="Image Error", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Pending"),
            ("approved", "Approved"),
            ("denied", "Denied"),
        ],
        default="draft",
    )
    approved_question_id = fields.Many2one(
        "etp.assessment.question", string="Bank Question", readonly=True
    )

    def action_generate_images(self):
        """Generate the A/B images for image_comparison drafts via Bedrock.

        One image model call per image (2 per question) — unavoidable, image
        APIs are single-prompt. Idempotent: already-generated drafts are
        skipped; failed ones can be retried.
        """
        from ..services import bedrock_images
        todo = self.filtered(
            lambda r: r.question_type == "image_comparison"
            and r.state == "draft"
            and r.image_state != "generated"
        )
        for rec in todo:
            if not (rec.image_prompt_a and rec.image_prompt_b):
                rec.write({
                    "image_state": "failed",
                    "image_error": "Missing image prompt A and/or B.",
                })
                continue
            try:
                img_a = bedrock_images.generate_image_b64(
                    self.env, rec.image_prompt_a
                )
                img_b = bedrock_images.generate_image_b64(
                    self.env, rec.image_prompt_b
                )
                rec.write({
                    "image_a": img_a,
                    "image_b": img_b,
                    "image_state": "generated",
                    "image_error": False,
                })
            except Exception as exc:
                _logger.exception(
                    "Image generation failed for draft question %s", rec.id
                )
                rec.write({
                    "image_state": "failed",
                    "image_error": str(exc)[:500],
                })
        return True

    def action_approve(self):
        """Create a real question in the Question Bank from this draft."""
        Question = self.env["etp.assessment.question"]
        for rec in self.filtered(lambda r: r.state == "draft"):
            vals = {
                "name": rec.name,
                "prompt": rec.question_prompt or rec.name,
                "question_type": rec.question_type or "text",
                "category_id": rec.prompt_id._get_or_create_category().id,
                "description": "Generated from prompt: %s (skill: %s)" % (
                    rec.prompt_id.name, rec.skill or "-"),
            }
            # carry generated images onto the bank question
            if rec.question_type == "image_comparison":
                if rec.image_a and rec.image_b:
                    vals["image_a"] = rec.image_a
                    vals["image_b"] = rec.image_b
                elif rec.image_prompt_a or rec.image_prompt_b:
                    # not generated yet — keep prompts visible for ops
                    vals["description"] = (vals["description"] +
                        "\n[Image prompts pending generation]"
                        "\nA: %s\nB: %s" % (
                            rec.image_prompt_a or "-",
                            rec.image_prompt_b or "-"))
            q = Question.create(vals)
            rec.write({"state": "approved", "approved_question_id": q.id})
        return True

    def action_deny(self):
        self.filtered(lambda r: r.state == "draft").write({"state": "denied"})
        return True

    def action_approve_all(self):
        """Approve every still-pending draft in this recordset (bulk)."""
        self.filtered(lambda r: r.state == "draft").action_approve()
        return True
