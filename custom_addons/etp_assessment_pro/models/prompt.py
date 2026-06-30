import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

from ..constants import (
    QUESTION_TYPE_SELECTION, DIFFICULTY_SELECTION, IMAGE_QUESTION_TYPES,
    option_name_reveals_reasoning, text_has_source_reference,
)

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
    extract_state = fields.Selection(
        [
            ("idle", "Idle"),
            ("queued", "Queued"),
            ("extracting", "Extracting"),
            ("done", "Extracted"),
            ("failed", "Failed"),
        ],
        default="idle", copy=False, string="Extraction State",
        help="Skill extraction runs in the background: clicking Extract Skills "
             "queues it and a cron does the (slow) LLM call OFF the web request. "
             "This is what prevents the 'cursor already closed' crash on managed "
             "Postgres, where a long in-request call lets the DB connection be "
             "reaped mid-flight.")
    extract_error = fields.Char(
        string="Extraction Error", readonly=True, copy=False)
    quick_upload_file = fields.Binary(string="Upload SOP / Doc")
    quick_upload_filename = fields.Char()
    upload_sop_file = fields.Binary(string="Upload SOP")
    upload_sop_filename = fields.Char()
    upload_vendor_file = fields.Binary(string="Upload Vendor Doc")
    upload_vendor_filename = fields.Char()
    upload_client_file = fields.Binary(string="Upload Client Doc")
    upload_client_filename = fields.Char()
    has_sop_resource = fields.Boolean(
        string="Has SOP", compute="_compute_has_sop_resource",
        help="True once at least one SOP document is attached. Drives the "
             "mandatory-SOP indicator: the upload stays required until a SOP "
             "resource exists (the upload field itself clears after each file, "
             "so the requirement tracks the resource, not the transient field).")

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

    @api.depends("resource_ids", "resource_ids.category")
    def _compute_has_sop_resource(self):
        for rec in self:
            rec.has_sop_resource = any(
                r.category == "sop" for r in rec.resource_ids)

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
        """Queue skill extraction for the background cron instead of running the
        LLM call inside this web request. A reasoning model can take minutes, and
        a long in-request call lets managed Postgres / pgbouncer reap the idle DB
        connection mid-flight, which crashes with 'cursor already closed'. The
        cron (`_cron_extract_pending_skills`) does the call off the request and
        commits the skills in its own transaction."""
        self.ensure_one()
        self.write({"extract_state": "queued", "extract_error": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Skill Extraction Queued",
                "message": "Extraction is running in the background. The skills "
                           "will appear here within about a minute — refresh the "
                           "page to see them.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _cron_extract_pending_skills(self):
        """Background drainer for queued skill extraction. The slow LLM call runs
        HERE, off the web request, and — critically — we COMMIT before it so the
        DB connection is NOT 'idle in a transaction' while we wait minutes for the
        model. A connection idle-in-transaction is what managed Postgres /
        pgbouncer reaps mid-flight, which is the 'cursor already closed' crash.
        Claiming a prompt as 'extracting' and committing also stops a second cron
        worker from re-picking it, so no advisory lock is needed."""
        from ..services import vertex
        prompts = self.search([("extract_state", "=", "queued")], limit=3)
        if not prompts:
            return
        _logger.info(
            "etp_assessment extract cron: %d queued prompt(s)", len(prompts))
        for prompt in prompts:
            # Claim + clear old skills, then COMMIT so the long call below holds
            # no open transaction (this is what avoids the connection reaper).
            prompt.write({"extract_state": "extracting", "extract_error": False})
            prompt.skill_ids.unlink()
            self.env.cr.commit()
            try:
                summary = vertex.extract_skills(self.env, prompt)
                prompt.write({
                    "state": "skills_ready",
                    "extract_state": "done",
                    "extract_error": False,
                    "last_extract_summary": "Created %s, Skipped %s, Total %s"
                    % (summary.get("created", 0), summary.get("skipped", 0),
                       summary.get("total", 0)),
                })
                self.env.cr.commit()
                _logger.info(
                    "etp_assessment extract cron: prompt %s -> %s",
                    prompt.id, prompt.last_extract_summary)
            except Exception as exc:  # noqa: BLE001 - isolate per prompt
                self.env.cr.rollback()
                _logger.exception(
                    "Skill extraction failed for prompt %s", prompt.id)
                prompt.write({
                    "extract_state": "failed", "extract_error": str(exc)[:300]})
                self.env.cr.commit()

    def action_generate_questions(self):
        """Queue per-skill question generation for the background cron. Each skill
        is a slow LLM call; running them in the web request lets managed Postgres
        reap the idle DB connection mid-flight ('cursor already closed'). The cron
        (`_cron_generate_pending_questions`) generates each skill OFF the request,
        committing before the call, with per-skill gen_state for isolation/retry."""
        self.ensure_one()
        if not self.selected_skill_ids:
            raise UserError(
                "Pick at least one skill from 'Skills to Generate For' before generating."
            )
        self.state = "generating"
        self.selected_skill_ids.write({
            "gen_state": "queued", "gen_error": False, "gen_prompt_id": self.id})
        _logger.info(
            "etp_assessment generate queued: prompt=%s skills=%d (%s)",
            self.id, len(self.selected_skill_ids),
            ", ".join(self.selected_skill_ids.mapped("name")))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Question Generation Queued",
                "message": "Generation is running in the background, one skill at "
                           "a time. Drafts will appear here within a minute or two "
                           "— refresh the page to see them.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _cron_generate_pending_questions(self):
        """Background drainer for queued question generation. Per skill we claim
        it, clear ONLY its old drafts, COMMIT (so the long LLM call holds no open
        transaction and dodges the managed-Postgres idle-in-transaction reaper),
        then generate. Per-skill isolation means a failing skill never blocks the
        others, and gen_state stays retryable. A prompt flips to 'done' once none
        of its skills are still pending."""
        from ..services import vertex
        Skill = self.env["etp.assessment.pro.skill"]
        skills = Skill.search([
            ("gen_state", "=", "queued"),
            ("gen_prompt_id", "!=", False),
        ], limit=3)
        if not skills:
            return
        _logger.info(
            "etp_assessment generate cron: %d queued skill(s)", len(skills))
        touched = self.browse()
        for skill in skills:
            prompt = skill.gen_prompt_id
            touched |= prompt
            # Claim + clear this skill's old drafts, then COMMIT before the call.
            skill.write({"gen_state": "generating", "gen_error": False})
            prompt.question_ids.filtered(
                lambda q: q.state == "draft" and q.skill_id.id == skill.id
            ).unlink()
            self.env.cr.commit()
            try:
                draft_ids = vertex.generate_questions(self.env, prompt, skill)
                count = len(draft_ids)
                skill.write({
                    "gen_state": "done" if count else "failed",
                    "gen_error": False if count
                    else "model returned no usable questions"})
                self.env.cr.commit()
                _logger.info(
                    "etp_assessment generate cron: skill %s -> %d draft(s)",
                    skill.name, count)
            except vertex.LLMRefusalError as exc:
                self.env.cr.rollback()
                _logger.warning(
                    "Generation declined for skill %s: %s", skill.name, exc)
                skill.write({"gen_state": "failed",
                             "gen_error": "declined: %s" % str(exc)[:200]})
                self.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - isolate per skill
                self.env.cr.rollback()
                _logger.exception(
                    "Generation failed for skill %s", skill.name)
                skill.write({"gen_state": "failed",
                             "gen_error": str(exc)[:200]})
                self.env.cr.commit()
        # Mark a prompt done once it has no more queued/generating skills.
        for prompt in touched:
            pending = Skill.search_count([
                ("gen_prompt_id", "=", prompt.id),
                ("gen_state", "in", ("queued", "generating"))])
            if not pending and prompt.state == "generating":
                prompt.write({"state": "done"})
                self.env.cr.commit()

    def action_retry_failed_skills(self):
        """Re-run generation for ONLY the skills whose last generation failed.
        The easy-recovery path: a transient model error on a couple of skills
        does not force regenerating (and losing) the whole bank."""
        self.ensure_one()
        failed = self.selected_skill_ids.filtered(
            lambda s: s.gen_state == "failed")
        if not failed:
            failed = self.skill_bank_ids.filtered(
                lambda s: s.gen_state == "failed")
        if not failed:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Nothing to retry",
                           "message": "No skills are in a failed state.",
                           "type": "info"},
            }
        previous = self.selected_skill_ids
        try:
            self.selected_skill_ids = [(6, 0, failed.ids)]
            return self.action_generate_questions()
        finally:
            self.selected_skill_ids = [(6, 0, previous.ids)]

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
        QUESTION_TYPE_SELECTION,
        default="mcq",
    )
    question_count = fields.Integer(default=5)
    time_minutes = fields.Integer(default=10)
    difficulty = fields.Selection(
        DIFFICULTY_SELECTION,
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
        QUESTION_TYPE_SELECTION,
        default="mcq",
    )
    medium_display = fields.Char(
        string="Medium", compute="_compute_medium_display",
        help="Image for the image question types (A/B Evaluation, "
             "Prompt/Labelling) — the only ones that render a picture; Text "
             "otherwise. Derived from the question type.")
    has_revealing_option = fields.Boolean(
        compute="_compute_has_revealing_option",
        help="True when an objective option name embeds its rationale "
             "(\"Image B, because ...\"). Option names are shown to the "
             "candidate, so the rationale must live in the hidden answer key.")
    has_source_reference = fields.Boolean(
        compute="_compute_has_source_reference",
        help="True when the question or its answer key cites the source "
             "material the candidate never sees (\"according to the SOP\", "
             "\"Section 2.1\"). Items must be self-contained.")
    difficulty = fields.Selection(
        DIFFICULTY_SELECTION,
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
    image_brief_json = fields.Text(
        string="Image Briefs (JSON)",
        help='JSON list of {"slot","label","prompt"} render briefs produced by '
             "the text model. The image model renders these on demand "
             "(Generate / Regenerate). Editable so Growth can tweak a brief "
             "before regenerating.")
    image_state = fields.Selection(
        [
            ("none", "No Images Needed"),
            ("pending", "Images Pending"),
            ("rendered", "Images Rendered"),
            ("uploaded", "Image Uploaded"),
            ("failed", "Render Failed"),
        ],
        default="none", string="Image State", copy=False,
        help="Lifecycle of this draft's images: pending = briefs exist but no "
             "picture yet; rendered = the image model produced them; uploaded "
             "= Growth supplied their own; failed = a render attempt failed.")
    upload_image = fields.Binary(
        string="Upload Replacement Image", attachment=True,
        help="Upload your own image when a generated one is wrong. Pick the "
             "slot, attach the file, then use 'Apply Uploaded Image'.")
    upload_slot = fields.Char(
        string="Upload Slot", default="a",
        help="Which slot the uploaded image replaces (a / b for A/B, or "
             "single / reference / output for prompt-labelling).")
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
    # Editable, presentable answer key (one axis per dimension; a single
    # "Answer" axis for MCQ/MSQ). Authoritative over the raw JSON once present:
    # _dimension_specs() reads these first, so the preview and approve follow
    # what the reviewer edits here. Seeded from the LLM JSON on create.
    answer_dimension_ids = fields.One2many(
        "etp.assessment.pro.prompt.question.dimension", "draft_id",
        string="Answer Key")
    # Editable rubric answer key (subjective_* + image_text). These mirror the
    # EXACT keys the grader reads out of subjective_rubric_json (services/
    # scoring.py), surfaced as friendly fields so a reviewer never edits raw
    # JSON. Computed from rubric_json and written straight back to it (other
    # keys preserved), so approve -> bank -> scoring is unchanged.
    ak_ideal_answer = fields.Text(
        string="Ideal Answer", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields",
        help="image_text: the model answer the candidate is graded against.")
    ak_mandatory_elements = fields.Text(
        string="Mandatory Elements", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields",
        help="One per line. Elements the answer MUST contain.")
    ak_penalty_rules = fields.Text(
        string="Penalty Rules", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields", help="One per line.")
    ak_scoring_guide = fields.Text(
        string="Scoring Guide", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields")
    ak_checklist = fields.Text(
        string="Checklist", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields",
        help="subjective_rubric: one required point per line.")
    ak_constraints = fields.Text(
        string="Constraints", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields", help="One per line.")
    ak_pass_condition = fields.Text(
        string="Pass Condition", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields")
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
    image_summary = fields.Char(
        string="Image Files", compute="_compute_image_summary",
        help="Human-readable summary of the stored images (slot, label, size). "
             "The raw base64 image data is never shown in the UI.")

    @api.depends("question_type")
    def _compute_medium_display(self):
        for rec in self:
            rec.medium_display = (
                "Image" if rec.question_type in IMAGE_QUESTION_TYPES else "Text")

    @api.depends("question_type", "options_json",
                 "answer_dimension_ids.option_line_ids.name")
    def _compute_has_revealing_option(self):
        for rec in self:
            flag = False
            if rec.question_type in ("mcq", "msq"):
                names = rec.answer_dimension_ids.option_line_ids.mapped("name")
                if not names and rec.options_json:
                    try:
                        names = json.loads(rec.options_json) or []
                    except (ValueError, TypeError):
                        names = []
                flag = any(option_name_reveals_reasoning(n) for n in names)
            rec.has_revealing_option = flag

    @api.depends("question_prompt", "official_reasoning", "options_json",
                 "rubric_json")
    def _compute_has_source_reference(self):
        for rec in self:
            rec.has_source_reference = text_has_source_reference(
                rec.question_prompt, rec.official_reasoning,
                rec.options_json, rec.rubric_json)

    @api.depends("images_json")
    def _compute_image_summary(self):
        import json as _json
        for rec in self:
            raw = (rec.images_json or "").strip()
            if not raw or raw in ("[]", "{}"):
                rec.image_summary = False
                continue
            try:
                specs = _json.loads(raw)
            except (ValueError, TypeError):
                rec.image_summary = "unparseable images_json"
                continue
            if isinstance(specs, dict):
                specs = [specs]
            parts = []
            for s in (specs or []):
                if not isinstance(s, dict):
                    continue
                src = s.get("data") or s.get("url") or ""
                if isinstance(src, str) and src.startswith("data:"):
                    kb = max(1, int(len(src) * 0.75 / 1024))
                    where = "rendered ~%dKB" % kb
                elif src:
                    where = "url"
                else:
                    where = "empty"
                parts.append("%s/%s [%s]" % (
                    s.get("slot") or "?", s.get("label") or "", where))
            rec.image_summary = "; ".join(parts) if parts else False

    @api.depends("dimensions_json", "options_json", "correct_answer_json",
                 "images_json", "question_type",
                 "answer_dimension_ids.label",
                 "answer_dimension_ids.sequence",
                 "answer_dimension_ids.option_line_ids.name",
                 "answer_dimension_ids.option_line_ids.is_correct")
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
                               else "badge text-bg-light border")
                        mark = " \u2713" if is_c else ""
                        style = ("margin:3px;font-size:0.95rem;"
                                 "padding:0.45em 0.7em;font-weight:%s"
                                 % ("600" if is_c else "400"))
                        opts.append(
                            f'<span class="{cls}" style="{style}">'
                            f'{_html.escape(o)}{mark}</span>')
                    blocks.append(
                        f'<div class="mb-2"><strong style="font-size:0.95rem">'
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

    # Map of friendly rubric field -> the JSON key the grader reads. Scalar
    # (prose) keys and list (one-per-line) keys are handled separately.
    _RUBRIC_STR_KEYS = (("ak_ideal_answer", "ideal_answer"),
                        ("ak_scoring_guide", "scoring_guide"),
                        ("ak_pass_condition", "pass_condition"))
    _RUBRIC_LIST_KEYS = (("ak_mandatory_elements", "mandatory_elements"),
                         ("ak_penalty_rules", "penalty_rules"),
                         ("ak_checklist", "checklist"),
                         ("ak_constraints", "constraints"))

    @api.depends("rubric_json")
    def _compute_answer_key_fields(self):
        import json as _json
        for rec in self:
            try:
                data = _json.loads(rec.rubric_json or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            for fname, key in rec._RUBRIC_STR_KEYS:
                rec[fname] = data.get(key) or False
            for fname, key in rec._RUBRIC_LIST_KEYS:
                val = data.get(key) or []
                if not isinstance(val, list):
                    val = [val]
                rec[fname] = "\n".join(str(x) for x in val) or False

    def _inverse_answer_key_fields(self):
        """Write the friendly rubric fields back into rubric_json, preserving
        any other keys. One shared inverse for all of them: it rebuilds from the
        current field values, so it is correct no matter how many fired."""
        import json as _json
        for rec in self:
            try:
                data = _json.loads(rec.rubric_json or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            for fname, key in rec._RUBRIC_STR_KEYS:
                if (rec[fname] or "").strip():
                    data[key] = rec[fname]
                else:
                    data.pop(key, None)
            for fname, key in rec._RUBRIC_LIST_KEYS:
                items = [ln.strip() for ln in (rec[fname] or "").splitlines()
                         if ln.strip()]
                if items:
                    data[key] = items
                else:
                    data.pop(key, None)
            rec.rubric_json = _json.dumps(data, ensure_ascii=False) if data else False

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

        The editable ``answer_dimension_ids`` records are authoritative when
        present (so the preview + approve follow what the reviewer edited);
        otherwise fall back to parsing the raw LLM JSON.
        """
        self.ensure_one()
        if self.answer_dimension_ids:
            specs = []
            for d in self.answer_dimension_ids:
                options = [o.name for o in d.option_line_ids if o.name]
                if not options:
                    continue
                correct = [o.name for o in d.option_line_ids
                           if o.is_correct and o.name]
                specs.append({
                    "label": (d.label or self.name or "Answer")[:200],
                    "options": options,
                    "correct": correct,
                })
            if specs:
                return specs
        return self._specs_from_json()

    def _specs_from_json(self):
        """Parse the raw LLM JSON answer key into dimension specs.

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
        or 0-based indices; return the matching option STRINGS.

        Indices may arrive as JSON ints (dimensions_json) OR as numeric strings
        (the plain ``correct_answer`` / ``dimN_correct`` CSV columns, which split
        to strings). String matching wins FIRST, so an option literally named
        "1" still matches by value; a numeric string is only treated as an index
        when it matches no option."""
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
                continue
            cs = str(c)
            # Exact match first, then case-insensitive.
            if cs in options:
                out.append(cs)
                continue
            matched = False
            for o in options:
                if o.strip().casefold() == cs.strip().casefold():
                    out.append(o)
                    matched = True
                    break
            if matched:
                continue
            # Fall back: a numeric string that matched no option is a 0-based
            # index (e.g. correct_answer="1" -> the 2nd option).
            cs_stripped = cs.strip()
            if cs_stripped.lstrip("-").isdigit():
                idx = int(cs_stripped)
                if 0 <= idx < len(options):
                    out.append(options[idx])
        return out

    def _sync_answer_relational_from_json(self):
        """(Re)build the editable answer_dimension_ids from the raw LLM JSON.
        Seeds the relational answer key on create and rebuilds it on demand
        after a raw-JSON edit. Types with no option set (subjective / image_text
        rubric) yield no axes, so they are simply left empty here."""
        for rec in self:
            specs = rec._specs_from_json()
            commands = [(5, 0, 0)]
            for si, spec in enumerate(specs):
                correct = {c.strip().casefold() for c in spec["correct"]}
                opt_cmds = [
                    (0, 0, {"name": o, "sequence": (i + 1) * 10,
                            "is_correct": o.strip().casefold() in correct})
                    for i, o in enumerate(spec["options"])
                ]
                commands.append((0, 0, {
                    "label": spec["label"], "sequence": (si + 1) * 10,
                    "option_line_ids": opt_cmds,
                }))
            rec.answer_dimension_ids = commands

    def action_rebuild_answer_key_from_json(self):
        """Button: overwrite the editable answer key from the raw JSON (use
        after hand-editing the Advanced raw JSON)."""
        self._sync_answer_relational_from_json()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Seed the editable answer key from whatever JSON the generator / import
        # wrote, unless the caller already supplied relational answer lines.
        for rec, vals in zip(records, vals_list):
            if rec.answer_dimension_ids:
                continue
            if (vals.get("dimensions_json") or vals.get("options_json")
                    or vals.get("correct_answer_json")):
                rec._sync_answer_relational_from_json()
        return records

    def _materialize_dimensions(self, bank_question):
        """Create one PRIVATE question.dimension per spec, flagging the correct
        option lines.

        FOOLPROOF IMPORT RULE: every imported/approved question gets its OWN
        fresh dimension carrying ONLY its own options — for ALL question types
        (mcq, msq, image_ab, image_text). We never look up a master dimension
        by name and never top up an existing one. Name-based reuse caused two
        real footguns:
          1. Accumulation — questions sharing a label (e.g. an Odoo export
             where every row is titled "Non-STEM Baseline", or two image_ab
             questions both using axis "Overall Choice") inherited each other's
             options (Q1=4, Q2=8 …) and even picked up stale options left on a
             pre-existing master.
          2. Cross-edit / drift — the per-question option line text is
             ``related(store=True)`` to the master, so editing one master option
             silently rewrites every question that borrowed it.
        A private dimension per question makes the answer set self-contained and
        immune to both. Analytics can still group by dimension NAME (preserved);
        it just no longer shares the row identity across questions.
        """
        self.ensure_one()
        Dimension = self.env["etp.assessment.pro.dimension"]
        QDim = self.env["etp.assessment.pro.question.dimension"]
        for spec in self._dimension_specs():
            options = spec["options"]
            if not options:
                continue
            label = spec["label"]
            # Always a brand-new dimension owning exactly this question's
            # options — no name lookup, no shared master, no top-up.
            dim = Dimension.with_context(
                allow_shared_dimension_edit=True).create({
                    "name": label,
                    "option_ids": [
                        (0, 0, {"name": o, "sequence": (i + 1) * 10})
                        for i, o in enumerate(options)
                    ],
                })
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

    # ------------------------------------------------------------------
    # Image lifecycle (Model 2 render + Growth controls). Decoupled from
    # generation so slow image calls never run in the generate request.
    # ------------------------------------------------------------------
    def _briefs(self):
        """Parse image_brief_json into a list of {slot,label,prompt}."""
        import json as _json
        self.ensure_one()
        raw = (self.image_brief_json or "").strip()
        if not raw or raw in ("[]", "{}"):
            return []
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        return [b for b in parsed if isinstance(b, dict)] \
            if isinstance(parsed, list) else []

    def _current_images(self):
        """Parse images_json into a list of {slot,label,data|url}."""
        import json as _json
        self.ensure_one()
        raw = (self.images_json or "").strip()
        if not raw or raw in ("[]", "{}"):
            return []
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        return [i for i in parsed if isinstance(i, dict)] \
            if isinstance(parsed, list) else []

    def _notify(self, title, message, kind="success", sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message,
                       "type": kind, "sticky": sticky},
        }

    def _render_all_images(self):
        """Render ALL of this draft's image briefs and store them. Returns True
        when at least one image was produced. Shared by the auto-render-on-
        generate path and the cron drainer."""
        import json as _json
        from ..services import vertex
        self.ensure_one()
        briefs = self._briefs()
        if not briefs:
            return False
        images = vertex.render_draft_images(
            self.env, briefs,
            usage_ctx={"operation": "generate_image",
                       "prompt_id": self.prompt_id.id,
                       "skill_id": self.skill_id.id, "note": self.name})
        if images:
            self.write({
                "images_json": _json.dumps(images, ensure_ascii=False),
                "image_state": "rendered",
            })
            return True
        self.write({"image_state": "failed"})
        return False

    @api.model
    def _cron_render_pending_images(self):
        """Background drainer: render image drafts that were created
        with briefs but no pixels yet (image_state='pending'). This is what makes
        the FIRST image generation automatic WITHOUT reintroducing the
        synchronous-render request-timeout crash — rendering happens here, off
        the web request, a few drafts per tick. Idempotent + advisory-locked so
        two cron workers never render the same draft twice.
        """
        # Advisory lock (distinct key from the scoring cron) so concurrent cron
        # workers don't double-render; auto-releases at commit/rollback.
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (827194,))
        if not self.env.cr.fetchone()[0]:
            return
        # Small batch per tick to stay well under any worker timeout even when a
        # large generation just produced many image drafts.
        drafts = self.search([
            ("question_type", "in", list(IMAGE_QUESTION_TYPES)),
            ("image_state", "=", "pending"),
            ("image_brief_json", "!=", False),
        ], limit=10)
        if not drafts:
            return
        _logger.info(
            "etp_assessment image cron: %d pending draft(s) to render", len(drafts))
        rendered = 0
        for draft in drafts:
            try:
                with self.env.cr.savepoint():
                    if draft._render_all_images():
                        rendered += 1
            except Exception:  # noqa: BLE001 - isolate per draft
                _logger.exception(
                    "Auto-render failed for draft %s", draft.id)
                draft.write({"image_state": "failed"})
                continue
        _logger.info(
            "etp_assessment image cron: rendered %d/%d draft(s)",
            rendered, len(drafts))

    def action_apply_uploaded_image(self):
        """Apply a Growth-uploaded binary as the image for upload_slot,
        replacing any generated image in that slot."""
        import json as _json
        self.ensure_one()
        if not self.upload_image:
            return self._notify(
                "No File", "Attach an image in 'Upload Replacement Image' "
                "first.", "warning")
        slot = (self.upload_slot or "a").strip().lower()
        raw_b64 = self.upload_image
        if isinstance(raw_b64, bytes):
            raw_b64 = raw_b64.decode("ascii", errors="ignore")
        data_url = "data:image/png;base64,%s" % raw_b64
        kept = [i for i in self._current_images()
                if (i.get("slot") or "").lower() != slot]
        kept.append({"slot": slot, "label": slot.title(), "data": data_url})
        self.write({
            "images_json": _json.dumps(kept, ensure_ascii=False),
            "image_state": "uploaded",
            "upload_image": False,
        })
        return self._notify(
            "Image Uploaded", "Your image now fills slot %r." % slot)


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
