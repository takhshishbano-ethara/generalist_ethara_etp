import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EtpAssessmentPrompt(models.Model):
    _name = "etp.assessment.pro.prompt"
    _description = "Assessment Prompt (LLM Skill/Question Generator)"
    _order = "create_date desc"

    name = fields.Char(string="Title", default="New Prompt", required=True)
    source_text = fields.Text(string="Additional Notes (optional)")
    resource_ids = fields.One2many(
        "etp.assessment.pro.prompt.resource", "prompt_id",
        string="SOP / Resource Files",
    )
    resource_count = fields.Integer(compute="_compute_resource_count")
    category_id = fields.Many2one(
        "etp.assessment.pro.category",
        string="Target Category",
    )
    skill_ids = fields.One2many(
        "etp.assessment.pro.prompt.skill", "prompt_id", string="Extracted (this run)"
    )
    skill_bank_ids = fields.Many2many(
        "etp.assessment.pro.skill",
        relation="etp_assessment_pro_prompt_skill_bank_rel",
        column1="prompt_id",
        column2="skill_id",
        string="Skill Bank Links",
    )
    selected_skill_ids = fields.Many2many(
        "etp.assessment.pro.skill",
        relation="etp_assessment_pro_prompt_selected_skill_rel",
        column1="prompt_id",
        column2="skill_id",
        string="Skills to Generate Questions For",
        help="Tick the skills you want question drafts for, then click "
             "Generate Questions. Each ticked skill triggers one LLM call.",
    )
    question_ids = fields.One2many(
        "etp.assessment.pro.prompt.question", "prompt_id", string="Draft Questions"
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
        Category = self.env["etp.assessment.pro.category"]
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
    _name = "etp.assessment.pro.prompt.skill"
    _description = "Prompt Extracted Skill (transient view)"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", required=True, ondelete="cascade"
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
            ("image_ab", "Image - A/B Evaluation"),
            ("image_text", "Image - Prompt/Labelling"),
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
        "etp.assessment.pro.skill", string="Bank Skill", ondelete="set null"
    )
    upsert_state = fields.Selection(
        [("created", "Created"), ("skipped", "Skipped (existed)")],
        readonly=True,
    )


class EtpAssessmentPromptQuestion(models.Model):
    _name = "etp.assessment.pro.prompt.question"
    _description = "Prompt Draft Question"
    _order = "id"

    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", required=True, ondelete="cascade"
    )
    skill_id = fields.Many2one(
        "etp.assessment.pro.skill", string="Skill", ondelete="set null"
    )
    name = fields.Char(string="Title", required=True)
    question_prompt = fields.Text(string="Question Prompt")
    description = fields.Text(
        string="Description",
        help="Optional candidate-facing description. NEVER put options or the "
             "correct answer here — those live in dimensions / the answer key.")
    category_id = fields.Many2one(
        "etp.assessment.pro.category", string="Target Category",
        help="Per-draft category override. Falls back to the generator's "
             "category when blank.")
    skill_names = fields.Char(
        string="Skill Names",
        help="Pipe-separated skill names from a CSV/JSON import; resolved to "
             "(or created as) bank skills on approve when skill_id is blank.")
    time_minutes = fields.Integer(string="Time (minutes)", default=0)
    question_type = fields.Selection(
        [
            ("mcq", "Objective - MCQ"),
            ("msq", "Objective - MSQ"),
            ("subjective_justification", "Subjective - Justification"),
            ("subjective_rubric", "Subjective - Rubric"),
            ("image_ab", "Image - A/B Evaluation"),
            ("image_text", "Image - Prompt/Labelling"),
        ],
        default="mcq",
    )
    difficulty = fields.Selection(
        [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
    )
    options_json = fields.Text(string="Options (JSON)")
    correct_answer_json = fields.Text(string="Correct Answer (JSON)")
    dimensions_json = fields.Text(
        string="Dimensions (JSON)",
        help="General multi-dimension answer key: a JSON list of "
             '{"label", "options":[..], "correct":[..]} objects. Used by '
             "image_ab (and any multi-objective question). When present it "
             "supersedes options_json/correct_answer_json. ``correct`` entries "
             "may be option strings OR 0-based indices into ``options``.")
    rubric_json = fields.Text(string="Rubric (JSON)")
    official_reasoning = fields.Text(
        string="Official Reasoning",
        help="image_ab: the official rationale the LLM grades the candidate's "
             "justification against.")
    images_json = fields.Text(
        string="Images (JSON)",
        help='JSON list of {"slot","label","url" | "data"} image specs. URLs '
             "(or base64 data) are uploaded to S3 on approve when S3 is "
             "configured, else the URL is stored as-is / the binary kept on "
             "the record.")
    state = fields.Selection(
        [
            ("draft", "Pending"),
            ("approved", "Approved"),
            ("denied", "Denied"),
        ],
        default="draft",
    )
    approved_question_id = fields.Many2one(
        "etp.assessment.pro.question", string="Bank Question", readonly=True
    )
    # Reviewer-facing previews so an imported draft READS like the real bank
    # question (images render, dimensions show as a clean list) instead of raw
    # JSON. The preview is derived from the same dimensions_json/images_json
    # that approve consumes, so what you see is what gets published.
    dimensions_preview = fields.Html(
        string="Answer Key Preview", compute="_compute_previews",
        sanitize=False)
    image_preview = fields.Html(
        string="Images", compute="_compute_previews", sanitize=False)
    has_images = fields.Boolean(compute="_compute_previews")
    has_dimensions = fields.Boolean(compute="_compute_previews")

    @api.depends("dimensions_json", "options_json", "correct_answer_json",
                 "images_json", "question_type")
    def _compute_previews(self):
        import html as _html
        for rec in self:
            # ---- dimensions / answer-key preview ----
            specs = rec._dimension_specs()
            if specs:
                blocks = []
                for spec in specs:
                    correct = {c.strip().casefold() for c in spec["correct"]}
                    opts = []
                    for o in spec["options"]:
                        is_c = o.strip().casefold() in correct
                        cls = ("badge text-bg-success" if is_c
                               else "badge text-bg-light")
                        mark = " \u2713" if is_c else ""
                        opts.append(
                            f'<span class="{cls}" style="margin:2px">'
                            f'{_html.escape(o)}{mark}</span>')
                    blocks.append(
                        f'<div class="mb-2"><strong>'
                        f'{_html.escape(spec["label"])}</strong><br/>'
                        f'{"".join(opts)}</div>')
                rec.dimensions_preview = "".join(blocks)
                rec.has_dimensions = True
            else:
                rec.dimensions_preview = False
                rec.has_dimensions = False
            # ---- image preview (URLs render; data: URLs render inline) ----
            imgs = []
            raw = (rec.images_json or "").strip()
            if raw and raw not in ("[]", "{}"):
                try:
                    parsed = __import__("json").loads(raw)
                except (ValueError, TypeError):
                    parsed = []
                if isinstance(parsed, dict):
                    parsed = [parsed]
                for spec in (parsed or []):
                    if not isinstance(spec, dict):
                        continue
                    src = spec.get("url") or spec.get("src") or spec.get("data")
                    if not src:
                        continue
                    label = _html.escape(str(
                        spec.get("label") or spec.get("slot") or ""))
                    imgs.append(
                        '<figure style="display:inline-block;margin:6px;'
                        'text-align:center">'
                        f'<img src="{_html.escape(src)}" '
                        'style="max-height:160px;max-width:220px;'
                        'border:1px solid #dee2e6;border-radius:4px"/>'
                        f'<figcaption class="text-muted small">{label}'
                        '</figcaption></figure>')
            rec.image_preview = "".join(imgs) if imgs else False
            rec.has_images = bool(imgs)

    def _resolve_skill_ids(self):
        """Resolve skill_id + any pipe-separated skill_names into bank skill
        ids, creating skills by name when missing (mirrors bank_import)."""
        self.ensure_one()
        ids = []
        if self.skill_id:
            ids.append(self.skill_id.id)
        names = [n.strip() for n in (self.skill_names or "").split("|") if n.strip()]
        if names:
            Skill = self.env["etp.assessment.pro.skill"]
            for name in names:
                sk = Skill.search([("name", "=", name)], limit=1) or Skill.create(
                    {"name": name})
                if sk.id not in ids:
                    ids.append(sk.id)
        return ids

    def action_approve(self):
        Question = self.env["etp.assessment.pro.question"]
        for rec in self.filtered(lambda r: r.state == "draft"):
            category = rec.category_id or rec.prompt_id._get_or_create_category()
            # description is candidate-facing prose; it must never carry the
            # options or correct answer (those live in dimensions + the
            # option_line is_correct flags / the rubric answer key).
            vals = {
                "name": rec.name,
                "prompt": rec.question_prompt or rec.name,
                "question_type": rec.question_type or "mcq",
                "category_id": category.id,
                "difficulty": rec.difficulty or False,
                "time_minutes": rec.time_minutes or 0,
                "description": rec.description or False,
                "subjective_rubric_json": rec.rubric_json or False,
                "official_reasoning": rec.official_reasoning or False,
                "source_ref": "gen:%s" % rec.prompt_id.name,
            }
            skill_ids = rec._resolve_skill_ids()
            if skill_ids:
                vals["skill_ids"] = [(6, 0, skill_ids)]
            q = Question.create(vals)
            if rec.question_type in ("mcq", "msq", "image_ab", "image_text"):
                rec._materialize_dimensions(q)
            if rec.question_type in ("image_ab", "image_text"):
                rec._materialize_images(q)
            rec.write({"state": "approved", "approved_question_id": q.id})
        return True

    def _dimension_specs(self):
        """Normalize this draft's answer key to a list of dimension specs:
        ``[{"label", "options":[str], "correct":[str]}]``.

        Source of truth is dimensions_json when present (multi-dimension,
        e.g. image_ab); otherwise fall back to the single-dimension
        options_json + correct_answer_json shorthand the generator emits.
        ``correct`` is normalized to option STRINGS (indices resolved here).
        """
        import json as _json
        self.ensure_one()
        specs = []
        raw = (self.dimensions_json or "").strip()
        if raw and raw not in ("[]", "{}"):
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                for d in parsed:
                    if not isinstance(d, dict):
                        continue
                    options = [str(o) for o in (d.get("options") or [])]
                    correct = self._normalize_correct(
                        d.get("correct"), options)
                    specs.append({
                        "label": (d.get("label") or d.get("key")
                                  or self.name or "Answer")[:200],
                        "options": options,
                        "correct": correct,
                    })
            if specs:
                return specs
        # Single-dimension shorthand.
        try:
            options = _json.loads(self.options_json or "[]")
        except (ValueError, TypeError):
            options = []
        if not isinstance(options, list) or not options:
            return specs
        options = [str(o) for o in options]
        try:
            correct = _json.loads(self.correct_answer_json or "null")
        except (ValueError, TypeError):
            correct = None
        specs.append({
            "label": (self.name or "Answer")[:200],
            "options": options,
            "correct": self._normalize_correct(correct, options),
        })
        return specs

    @staticmethod
    def _normalize_correct(correct, options):
        """Accept correct answers as a single value, a list, option strings,
        or 0-based indices; return the matching option STRINGS."""
        if correct is None:
            return []
        if not isinstance(correct, list):
            correct = [correct]
        out = []
        for c in correct:
            if isinstance(c, bool):
                continue
            if isinstance(c, int) and 0 <= c < len(options):
                out.append(options[c])
            else:
                cs = str(c)
                # Exact match first, then case-insensitive.
                if cs in options:
                    out.append(cs)
                else:
                    for o in options:
                        if o.strip().casefold() == cs.strip().casefold():
                            out.append(o)
                            break
        return out

    def _materialize_dimensions(self, bank_question):
        """Create one question.dimension per spec, reusing master dimensions
        by label and flagging the correct option lines."""
        self.ensure_one()
        Dimension = self.env["etp.assessment.pro.dimension"]
        QDim = self.env["etp.assessment.pro.question.dimension"]
        for spec in self._dimension_specs():
            options = spec["options"]
            if not options:
                continue
            label = spec["label"]
            dim = Dimension.search([("name", "=", label)], limit=1)
            if not dim:
                dim = Dimension.create({
                    "name": label,
                    "option_ids": [
                        (0, 0, {"name": o, "sequence": (i + 1) * 10})
                        for i, o in enumerate(options)
                    ],
                })
            else:
                existing = set(dim.option_ids.mapped("name"))
                missing = [o for o in options if o not in existing]
                if missing:
                    dim.write({"option_ids": [
                        (0, 0, {"name": o}) for o in missing]})
            qd = QDim.create({
                "question_id": bank_question.id,
                "dimension_id": dim.id,
            })
            correct = {c.strip().casefold() for c in spec["correct"]}
            if correct:
                for line in qd.option_line_ids:
                    if (line.name or "").strip().casefold() in correct:
                        line.write({"is_correct": True})
            elif options:
                _logger.warning(
                    "Draft %s dim %r: no correct option resolved; approved "
                    "with no answer key for this dimension.", self.id, label)

    def _materialize_images(self, bank_question):
        """Create question.image rows from images_json, pushing URLs / base64
        data to S3 when configured (graceful fallback to a raw URL or binary)."""
        import json as _json
        self.ensure_one()
        raw = (self.images_json or "").strip()
        if not raw or raw in ("[]", "{}"):
            return
        try:
            specs = _json.loads(raw)
        except (ValueError, TypeError):
            _logger.warning("Draft %s: images_json not parseable, skipped.",
                            self.id)
            return
        if isinstance(specs, dict):
            specs = [specs]
        if not isinstance(specs, list):
            return
        from ..services import image_ingest
        Image = self.env["etp.assessment.pro.question.image"]
        default_slot = "a" if bank_question.question_type == "image_ab" \
            else "single"
        # Normalize author-friendly slot spellings to the model's Selection
        # keys so "A"/"B"/"Single"/"Ref"/"Output" all import cleanly instead
        # of raising a Selection ValueError mid-import.
        valid_slots = {"a", "b", "single", "reference", "output"}
        slot_aliases = {
            "a": "a", "response a": "a", "resp a": "a", "image a": "a",
            "b": "b", "response b": "b", "resp b": "b", "image b": "b",
            "single": "single", "image": "single", "img": "single",
            "ref": "reference", "reference": "reference",
            "out": "output", "output": "output",
        }

        def _norm_slot(value):
            key = (value or "").strip().lower()
            if key in valid_slots:
                return key
            return slot_aliases.get(key, default_slot)

        for idx, spec in enumerate(specs):
            if not isinstance(spec, dict):
                continue
            slot = _norm_slot(spec.get("slot"))
            label = spec.get("label") or slot.title()
            vals = {
                "question_id": bank_question.id,
                "slot": slot,
                "label": label,
                "sequence": (idx + 1) * 10,
            }
            url, b64 = image_ingest.ingest(
                self.env, spec.get("url") or spec.get("src"),
                spec.get("data"),
                key_hint="qimg-%s-%s" % (bank_question.id, slot))
            if url:
                vals["image_url"] = url
            if b64:
                vals["image"] = b64
            Image.create(vals)

    def action_deny(self):
        self.filtered(lambda r: r.state == "draft").write({"state": "denied"})
        return True

    def action_approve_all(self):
        self.filtered(lambda r: r.state == "draft").action_approve()
        return True


class EtpAssessmentPromptResource(models.Model):
    _name = "etp.assessment.pro.prompt.resource"
    _description = "Prompt Resource File"
    _order = "sequence, id"

    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", required=True, ondelete="cascade"
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
