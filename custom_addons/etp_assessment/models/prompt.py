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
        string="Additional Notes (optional)",
        help="Optional extra instructions appended after the uploaded "
             "resource files in the generation payload.",
    )
    resource_ids = fields.One2many(
        "etp.assessment.prompt.resource", "prompt_id",
        string="SOP / Resource Files",
        help="Upload one or more resource documents (SOP, instruction docs, "
             "guidelines). Their extracted text is concatenated and fed to "
             "the seed prompt.",
    )
    resource_count = fields.Integer(compute="_compute_resource_count")
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

    @api.depends("resource_ids")
    def _compute_resource_count(self):
        for rec in self:
            rec.resource_count = len(rec.resource_ids)

    def _compiled_source_text(self):
        """All uploaded resources' extracted text + optional notes, in order.

        This is THE input the seed prompt sees. Raises UserError when no
        usable text exists at all.
        """
        self.ensure_one()
        parts = []
        for res in self.resource_ids.sorted("sequence"):
            text = (res.extracted_text or "").strip()
            if text:
                parts.append(
                    "===== RESOURCE: %s =====\n%s" % (res.name or "file", text)
                )
            elif res.extraction_error:
                _logger.warning(
                    "Prompt %s: resource '%s' has no extracted text (%s)",
                    self.id, res.name, res.extraction_error,
                )
        if (self.source_text or "").strip():
            parts.append(
                "===== ADDITIONAL NOTES =====\n%s" % self.source_text.strip()
            )
        if not parts:
            raise UserError(
                "No source material. Upload at least one resource file "
                "(or add notes) before generating."
            )
        return "\n\n".join(parts)

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
            data["resources"] = [r.to_dict() for r in self.resource_ids]
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
        from ..services.vertex_questions import (
            DEFAULT_SEED_PROMPT, _load_bundled_prompt,
        )
        p = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.seed_system_prompt", "") or ""
        if p.strip():
            return p
        bundled = _load_bundled_prompt("seed_master.md")
        return bundled if bundled.strip() else DEFAULT_SEED_PROMPT

    @api.model
    def _get_skills_system_prompt(self):
        from ..services.vertex_questions import DEFAULT_SKILLS_PROMPT
        return self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.skills_system_prompt", "") or DEFAULT_SKILLS_PROMPT

    @api.model
    def _get_questions_system_prompt(self):
        from ..services.vertex_questions import DEFAULT_QUESTIONS_PROMPT
        return self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment.questions_system_prompt", "") or DEFAULT_QUESTIONS_PROMPT

    # ---- RPC entry points used by the Prompt UI ----
    def action_extract_skills(self):
        """Call the LLM, replace skills, return them for progressive render."""
        from ..services import vertex_questions
        self.ensure_one()
        self.skill_ids.unlink()
        skills = vertex_questions.extract_skills(
            self.env, self._compiled_source_text(), self._get_skills_system_prompt()
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
        """Generate draft questions. ONE Vertex AI Gemini call either way.

        seed mode (default, research-team flow): SOP + golden example ->
        questions directly, no skills stage.
        skills mode (legacy): uses previously extracted skills.
        """
        from ..services import vertex_questions
        self.ensure_one()
        # clear previous drafts (keep approved/denied history? -> drop drafts only)
        self.question_ids.filtered(lambda q: q.state == "draft").unlink()

        if self.generation_mode == "seed":
            if not (self.golden_example or "").strip():
                raise UserError(
                    "Seed mode needs a Golden Example Question — paste one "
                    "exemplary question that shows the desired style and depth."
                )
            qs = vertex_questions.generate_questions_from_seed(
                self.env,
                self._compiled_source_text(),
                self.golden_example,
                self._get_seed_system_prompt(),
                max_questions=self.max_questions or 0,
            )
        else:
            skills = [
                {"name": s.name, "max_questions": s.max_questions}
                for s in self.skill_ids
            ]
            qs = vertex_questions.generate_questions(
                self.env, self._compiled_source_text(), skills,
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
            ("subjective", "Subjective"),
        ],
        default="text",
    )
    # --- image_comparison support: LLM emits two text-to-image prompts,
    # vertex_images turns them into actual images before/at approval ---
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
    image_a_url = fields.Char(string="Image A URL (S3)")
    image_b_url = fields.Char(string="Image B URL (S3)")
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
        """Generate the A/B images for image_comparison drafts via Vertex.

        Each generated image is uploaded to S3 (when configured) and its URL
        stored on the draft; otherwise the base64 is kept inline on the
        record. Idempotent: already-generated drafts are skipped; failed ones
        can be retried.
        """
        from ..services import vertex_images, s3_service
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
                img_a = vertex_images.generate_image_b64(
                    self.env, rec.image_prompt_a
                )
                img_b = vertex_images.generate_image_b64(
                    self.env, rec.image_prompt_b
                )
                vals = {"image_state": "generated", "image_error": False}
                # upload to S3 when configured, else keep inline binary.
                # upload_image_pair is atomic: if B fails after A, it rolls
                # back A so no orphan is left in the bucket.
                if s3_service.is_configured(self.env):
                    url_a, url_b = s3_service.upload_image_pair(
                        self.env, img_a, img_b, key_hint=f"gen{rec.id}")
                    vals["image_a_url"] = url_a
                    vals["image_b_url"] = url_b
                else:
                    vals["image_a"] = img_a
                    vals["image_b"] = img_b
                rec.write(vals)
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
            # carry generated images onto the bank question (URL preferred)
            if rec.question_type == "image_comparison":
                if rec.image_a_url and rec.image_b_url:
                    vals["image_a_url"] = rec.image_a_url
                    vals["image_b_url"] = rec.image_b_url
                elif rec.image_a and rec.image_b:
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


class EtpAssessmentPromptResource(models.Model):
    """One uploaded resource file (SOP, instruction doc...) on a prompt.

    Text is extracted at upload time so generation never re-parses files.
    Supported: .docx (stdlib zip+xml, no python-docx dependency), .txt,
    .md, .csv, .html. PDFs and images record an extraction error — convert
    to docx/txt or paste into Additional Notes.
    """
    _name = "etp.assessment.prompt.resource"
    _description = "Prompt Resource File"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Filename")
    file = fields.Binary(string="File", attachment=True, required=True)
    extracted_text = fields.Text(string="Extracted Text", readonly=True)
    extraction_error = fields.Char(string="Extraction Issue", readonly=True)
    char_count = fields.Integer(
        string="Characters", compute="_compute_char_count", store=True
    )

    @api.depends("extracted_text")
    def _compute_char_count(self):
        for rec in self:
            rec.char_count = len(rec.extracted_text or "")

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_docx(raw):
        """Pull paragraph text out of a .docx using stdlib only."""
        import io
        import re as _re
        import zipfile
        # defusedxml: docx files are user-uploaded; stdlib ElementTree is
        # vulnerable to XXE / entity-expansion payloads.
        try:
            from defusedxml.ElementTree import fromstring as _xml_fromstring
        except ImportError:
            from xml.etree.ElementTree import (  # noqa: S314 — fallback only
                fromstring as _xml_fromstring,
            )

        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = _xml_fromstring(xml_bytes)
        paragraphs = []
        for p in root.iter(f"{ns}p"):
            texts = [t.text or "" for t in p.iter(f"{ns}t")]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
        text = "\n".join(paragraphs)
        return _re.sub(r"\n{3,}", "\n\n", text)

    def _extract_text(self):
        """Best-effort text extraction by filename extension."""
        self.ensure_one()
        import base64

        raw = base64.b64decode(self.file or b"")
        if not raw:
            return "", "Empty file."
        ext = (self.name or "").rsplit(".", 1)[-1].lower()
        try:
            if ext == "docx":
                return self._extract_docx(raw), False
            if ext in ("txt", "md", "csv", "html", "htm", "json", "xml"):
                text = raw.decode("utf-8", errors="replace")
                if ext in ("html", "htm"):
                    import re as _re
                    text = _re.sub(r"<[^>]+>", " ", text)
                    text = _re.sub(r"\s+", " ", text)
                return text, False
            if ext == "pdf":
                return "", ("PDF text extraction not supported — convert to "
                            ".docx/.txt or paste the text into Additional Notes.")
            return "", f"Unsupported file type '.{ext}'."
        except Exception as exc:
            _logger.exception("Resource extraction failed: %s", self.name)
            return "", str(exc)[:300]

    def _run_extraction(self):
        for rec in self:
            text, error = rec._extract_text()
            rec.write({
                "extracted_text": text or False,
                "extraction_error": error or False,
            })

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._run_extraction()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "file" in vals or "name" in vals:
            self._run_extraction()
        return res

    def to_dict(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name or "",
            "sequence": self.sequence or 0,
            "char_count": self.char_count,
            "extraction_error": self.extraction_error or "",
        }
