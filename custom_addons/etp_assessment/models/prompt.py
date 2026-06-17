import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EtpAssessmentPrompt(models.Model):
    _name = "etp.assessment.prompt"
    _description = "Assessment Prompt (LLM Skill/Question Generator)"
    _order = "create_date desc"

    name = fields.Char(string="Title", default="New Prompt", required=True)
    source_text = fields.Text(string="Additional Notes (optional)")
    resource_ids = fields.One2many(
        "etp.assessment.prompt.resource", "prompt_id",
        string="SOP / Resource Files",
    )
    resource_count = fields.Integer(compute="_compute_resource_count")
    category_id = fields.Many2one(
        "etp.assessment.category",
        string="Target Category",
    )
    skill_ids = fields.One2many(
        "etp.assessment.prompt.skill", "prompt_id", string="Extracted (this run)"
    )
    skill_bank_ids = fields.Many2many(
        "etp.assessment.skill",
        relation="etp_assessment_prompt_skill_bank_rel",
        column1="prompt_id",
        column2="skill_id",
        string="Skill Bank Links",
    )
    selected_skill_ids = fields.Many2many(
        "etp.assessment.skill",
        relation="etp_assessment_prompt_selected_skill_rel",
        column1="prompt_id",
        column2="skill_id",
        string="Skills to Generate Questions For",
        help="Tick the skills you want question drafts for, then click "
             "Generate Questions. Each ticked skill triggers one LLM call.",
    )
    question_ids = fields.One2many(
        "etp.assessment.prompt.question", "prompt_id", string="Draft Questions"
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("skills_ready", "Skills Extracted"),
            ("generating", "Generating"),
            ("done", "Done"),
        ],
        default="draft",
    )
    question_count = fields.Integer(compute="_compute_counts")
    approved_count = fields.Integer(compute="_compute_counts")
    last_extract_summary = fields.Char(readonly=True)
    quick_upload_file = fields.Binary(string="Upload SOP / Doc")
    quick_upload_filename = fields.Char()
    upload_sop_file = fields.Binary(string="Upload SOP")
    upload_sop_filename = fields.Char()
    upload_vendor_file = fields.Binary(string="Upload Vendor Doc")
    upload_vendor_filename = fields.Char()
    upload_client_file = fields.Binary(string="Upload Client Doc")
    upload_client_filename = fields.Char()

    def _add_resource(self, data, filename, category):
        if not data:
            return
        self.resource_ids = [(0, 0, {
            "name": filename or "uploaded-file",
            "file": data,
            "category": category,
        })]

    @api.onchange("quick_upload_file")
    def _onchange_quick_upload_file(self):
        self._add_resource(
            self.quick_upload_file, self.quick_upload_filename, "other",
        )
        self.quick_upload_file = False
        self.quick_upload_filename = False

    @api.onchange("upload_sop_file")
    def _onchange_upload_sop(self):
        self._add_resource(
            self.upload_sop_file, self.upload_sop_filename, "sop",
        )
        self.upload_sop_file = False
        self.upload_sop_filename = False

    @api.onchange("upload_vendor_file")
    def _onchange_upload_vendor(self):
        self._add_resource(
            self.upload_vendor_file, self.upload_vendor_filename, "vendor",
        )
        self.upload_vendor_file = False
        self.upload_vendor_filename = False

    @api.onchange("upload_client_file")
    def _onchange_upload_client(self):
        self._add_resource(
            self.upload_client_file, self.upload_client_filename, "client",
        )
        self.upload_client_file = False
        self.upload_client_filename = False

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

    def _get_or_create_category(self):
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

    def action_extract_skills(self):
        self.ensure_one()
        from ..services import vertex
        self.skill_ids.unlink()
        summary = vertex.extract_skills(self.env, self)
        self.write({
            "state": "skills_ready",
            "last_extract_summary": "Created %s, Skipped %s, Total %s" % (
                summary.get("created", 0),
                summary.get("skipped", 0),
                summary.get("total", 0),
            ),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Skill Extraction Complete",
                "message": "Created %s new, skipped %s existing (of %s extracted)." % (
                    summary.get("created", 0),
                    summary.get("skipped", 0),
                    summary.get("total", 0),
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_generate_questions(self):
        self.ensure_one()
        from ..services import vertex
        if not self.selected_skill_ids:
            raise UserError(
                "Pick at least one skill from 'Skills to Generate For' before generating."
            )
        self.question_ids.filtered(lambda q: q.state == "draft").unlink()
        self.state = "generating"
        total = 0
        for skill in self.selected_skill_ids:
            draft_ids = vertex.generate_questions(self.env, self, skill)
            total += len(draft_ids)
        self.state = "done"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Question Drafts Ready",
                "message": "Generated %s drafts across %s skill(s)." % (
                    total, len(self.selected_skill_ids)
                ),
                "type": "success",
                "sticky": False,
            },
        }


class EtpAssessmentPromptSkill(models.Model):
    _name = "etp.assessment.prompt.skill"
    _description = "Prompt Extracted Skill (transient view)"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    name = fields.Char(string="Skill", required=True)
    description = fields.Text()
    tags = fields.Char()
    sequence = fields.Integer(default=10)
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
        ],
        default="mcq",
    )
    question_count = fields.Integer(default=5)
    time_minutes = fields.Integer(default=10)
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        default="medium",
    )
    bank_skill_id = fields.Many2one(
        "etp.assessment.skill", string="Bank Skill", ondelete="set null"
    )
    upsert_state = fields.Selection(
        [("created", "Created"), ("skipped", "Skipped (existed)")],
        readonly=True,
    )


class EtpAssessmentPromptQuestion(models.Model):
    _name = "etp.assessment.prompt.question"
    _description = "Prompt Draft Question"
    _order = "id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    skill_id = fields.Many2one(
        "etp.assessment.skill", string="Skill", ondelete="set null"
    )
    name = fields.Char(string="Title", required=True)
    question_prompt = fields.Text(string="Question Prompt")
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
        ],
        default="mcq",
    )
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
    )
    options_json = fields.Text(string="Options (JSON)")
    correct_answer_json = fields.Text(string="Correct Answer (JSON)")
    rubric_json = fields.Text(string="Rubric (JSON)")
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

    def action_approve(self):
        Question = self.env["etp.assessment.question"]
        for rec in self.filtered(lambda r: r.state == "draft"):
            category = rec.prompt_id._get_or_create_category()
            description_parts = []
            if rec.options_json:
                description_parts.append("Options: %s" % rec.options_json)
            if rec.correct_answer_json:
                description_parts.append("Correct: %s" % rec.correct_answer_json)
            description = "\n".join(description_parts) if description_parts else False
            vals = {
                "name": rec.name,
                "prompt": rec.question_prompt or rec.name,
                "question_type": rec.question_type or "mcq",
                "category_id": category.id,
                "difficulty": rec.difficulty or False,
                "description": description,
                "subjective_rubric_json": rec.rubric_json or False,
                "source_ref": "gen:%s" % rec.prompt_id.name,
            }
            if rec.skill_id:
                vals["skill_ids"] = [(4, rec.skill_id.id)]
            q = Question.create(vals)
            if rec.question_type in ("mcq", "msq"):
                rec._materialize_dimension(q)
            rec.write({"state": "approved", "approved_question_id": q.id})
        return True

    def _materialize_dimension(self, bank_question):
        import json as _json
        self.ensure_one()
        try:
            options = _json.loads(self.options_json or "[]")
        except (ValueError, TypeError):
            return
        if not isinstance(options, list) or not options:
            return
        try:
            correct = _json.loads(self.correct_answer_json or "null")
        except (ValueError, TypeError):
            correct = None
        if isinstance(correct, int):
            correct_indices = {correct}
        elif isinstance(correct, list):
            correct_indices = {i for i in correct if isinstance(i, int)}
        else:
            correct_indices = set()

        Dimension = self.env["etp.assessment.dimension"]
        Option = self.env["etp.assessment.dimension.option"]
        dim_name = (self.name or bank_question.name or "Answer")[:200]
        dimension = Dimension.create({"name": dim_name})
        master_options = []
        for idx, opt_text in enumerate(options):
            master_options.append(Option.create({
                "name": str(opt_text)[:200],
                "dimension_id": dimension.id,
                "sequence": (idx + 1) * 10,
            }))

        qd_vals = {
            "question_id": bank_question.id,
            "dimension_id": dimension.id,
            "option_line_ids": [
                (0, 0, {
                    "master_option_id": mo.id,
                    "is_correct": idx in correct_indices,
                    "sequence": (idx + 1) * 10,
                })
                for idx, mo in enumerate(master_options)
            ],
        }
        self.env["etp.assessment.question.dimension"].create(qd_vals)

    def action_deny(self):
        self.filtered(lambda r: r.state == "draft").write({"state": "denied"})
        return True

    def action_approve_all(self):
        self.filtered(lambda r: r.state == "draft").action_approve()
        return True


class EtpAssessmentPromptResource(models.Model):
    _name = "etp.assessment.prompt.resource"
    _description = "Prompt Resource File"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.prompt", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Filename")
    category = fields.Selection(
        [("sop", "SOP"), ("vendor", "Vendor"),
         ("client", "Client"), ("other", "Other")],
        default="other", required=True, index=True,
    )
    file = fields.Binary(string="File", attachment=True, required=True)
    extracted_text = fields.Text(readonly=True)
    extraction_error = fields.Char(readonly=True)
    char_count = fields.Integer(
        compute="_compute_char_count", store=True
    )

    @api.depends("extracted_text")
    def _compute_char_count(self):
        for rec in self:
            rec.char_count = len(rec.extracted_text or "")

    @staticmethod
    def _extract_docx(raw):
        import io
        import re as _re
        import zipfile
        try:
            from defusedxml.ElementTree import fromstring as _xml_fromstring
        except ImportError:
            from xml.etree.ElementTree import (  # noqa: S314
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
                return "", ("PDF text extraction not supported - convert to "
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
