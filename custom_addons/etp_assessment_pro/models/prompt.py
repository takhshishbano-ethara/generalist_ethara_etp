import json
import logging

from markupsafe import Markup, escape

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

from ..constants import (
    QUESTION_TYPE_SELECTION, DIFFICULTY_SELECTION, IMAGE_QUESTION_TYPES,
    VIDEO_QUESTION_TYPES, DETECTION_MODE_SELECTION,
    option_name_reveals_reasoning, text_has_source_reference,
    ADVISORY_LOCK_SKILL_EXTRACT, ADVISORY_LOCK_QUESTION_GEN,
    ADVISORY_LOCK_IMAGE_RENDER, ADVISORY_LOCK_TAG_EXTRACT,
    ADVISORY_LOCK_VIDEO_POLL,
    TAG_PREFIX_WEIGHTS, TAG_DEFAULT_PREFIX_WEIGHT,
    TAG_SIMILAR_MIN_SCORE_DEFAULT,
    ab_construction_keys, ab_code_from_label, ab_key_drift,
    parse_flaw_plan,
)

_logger = logging.getLogger(__name__)

_SOP_GEN_FINALIZE_MAX_ATTEMPTS = 5

_TAG_EXTRACT_FINALIZE_MAX_ATTEMPTS = 5


def _preview_src_ok(src):
    """L-2: allow only safe image sources in admin-preview markup — http(s), our
    own relative controller paths (/...), or image data: URLs. Rejects
    javascript:/vbscript:/data-non-image and other schemes so an LLM- or
    admin-supplied spec can never inject an active-content URL, even if this
    markup is later refactored from <img src> to an href-bearing tag."""
    s = str(src or "").strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith(("http://", "https://", "/")):
        return True
    if low.startswith("data:image/"):
        return True
    return False

_VIDEO_OP_MAX_ATTEMPTS = 3


class EtpAssessmentPrompt(models.Model):
    _name = "etp.assessment.pro.prompt"
    _description = "Assessment Prompt (LLM Skill/Question Generator)"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    def _log_activity(self, body):
        """Post a timestamped audit line to each generator's chatter.

        message_post already stamps the author + date, so this is the single
        choke-point every user action routes through to keep the log uniform.
        Best-effort: an audit note must never sink the action that triggered it.
        """
        for rec in self:
            if not rec.id:
                continue
            try:
                rec.message_post(body=body, message_type="comment",
                                 subtype_xmlid="mail.mt_note")
            except Exception:  # noqa: BLE001 - logging is never load-bearing
                _logger.exception(
                    "Chatter audit post failed for generator %s", rec.id)

    name = fields.Char(string="Title", default="New Prompt", required=True)
    source_text = fields.Text(string="Additional Notes (optional)")
    resource_ids = fields.One2many(
        "etp.assessment.pro.prompt.resource", "prompt_id",
        string="SOP / Resource Files",
    )
    reference_resource_ids = fields.One2many(
        "etp.assessment.pro.prompt.resource", "prompt_id",
        domain=[("category", "=", "reference")],
        string="Reference Files",
    )
    # Must stay computed: a sibling domain One2many cannot see the rows
    # _add_resource() appends to resource_ids until the record is saved.
    sop_resource_ids = fields.Many2many(
        "etp.assessment.pro.prompt.resource",
        compute="_compute_sop_resource_ids",
        string="SOP File(s)",
    )
    resource_count = fields.Integer(compute="_compute_resource_count")

    question_ids = fields.One2many(
        "etp.assessment.pro.prompt.question", "prompt_id", string="Draft Questions"
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("skills_ready", "Tags Extracted"),
            ("generating", "Generating"),
            ("done", "Done"),
        ],
        default="draft",
    )
    question_count = fields.Integer(compute="_compute_counts")
    approved_count = fields.Integer(compute="_compute_counts")
    last_extract_summary = fields.Char(readonly=True)
    sop_gen_state = fields.Selection(
        [("idle", "Idle"), ("queued", "Queued"), ("generating", "Generating"),
         ("finalizing", "Finalizing"),
         ("done", "Done"), ("failed", "Failed")],
        default="idle", copy=False, string="SOP Generation",
        help="The SOP document is sent natively to the "
             "multimodal model which authors questions directly per the format "
             "in the SOP. Runs in the background (a cron does the slow call off "
             "the web request). 'finalizing' means the drafts are committed and "
             "only the status write remains — a killed worker resumes WITHOUT "
             "re-calling Vertex.")
    sop_gen_error = fields.Char(
        string="SOP Generation Error", readonly=True, copy=False)
    tag_ids = fields.Many2many("etp.assessment.pro.tag", string="SOP Tags")
    tag_extract_state = fields.Selection(
        [("idle", "Idle"), ("queued", "Queued"), ("generating", "Generating"),
         ("done", "Done"), ("failed", "Failed")],
        default="idle", copy=False, string="Tag Extraction",
        help="Semantic tags characterizing the SOP's task are extracted by the "
             "LLM in the background (a cron does the slow call off the web "
             "request), mirroring SOP generation.")
    tag_extract_error = fields.Char(
        string="Tag Extraction Error", readonly=True, copy=False)
    tags_json = fields.Text(
        string="Tags (raw LLM output)", readonly=True, copy=False,
        help="Raw JSON array returned by the tag-extraction call, kept for "
             "debugging; the canonicalized tags live in tag_ids.")
    metadata_json = fields.Text(
        string="SOP Metadata (research schema)", readonly=True, copy=False,
        help="The full grounded metadata object research's seed prompt emits: "
             "sop_title, summary, mapping, tags, skills, evidence, "
             "required_elements, covered_by_all, question_spec, gaps.")
    plain_tags = fields.Char(
        string="Plain Tags", readonly=True, copy=False,
        help="The 3-4 unprefixed defining-trait tags (research 'tags' artifact), "
             "distinct from the faceted mapping stored in tag_ids.")
    required_elements_json = fields.Text(
        string="Required Elements (JSON)", readonly=True, copy=False,
        help="Atomic yes/no SOP requirements [{id,statement,evidence}] the "
             "questions cover; load-bearing for scoring SOP-Coverage.")
    covered_by_all_json = fields.Text(
        string="Covered By All (JSON)", readonly=True, copy=False,
        help="Required-element ids every question exercises (the coverage "
             "baseline unioned with each question's covers_elements).")
    sop_title = fields.Char(
        string="SOP Title", readonly=True, copy=False,
        help="The project title the SOP intake recovered (metadata.sop_title).")
    sop_summary = fields.Text(
        string="SOP Summary", readonly=True, copy=False,
        help="Two or three plain sentences on what workers do (metadata.summary).")
    mapping_json = fields.Text(
        string="Faceted Mapping (JSON)", readonly=True, copy=False,
        help="The full faceted profile [facet:value] the intake extracted; "
             "reconciled into tag_ids so it drives the weighted-Jaccard ranking.")
    skills_json = fields.Text(
        string="Skills Registry (JSON)", readonly=True, copy=False,
        help="[{id,name,weight 1-5,evidence}] - the SOP's skills and their "
             "centrality weight, kept for ranking and reporting.")
    evidence_json = fields.Text(
        string="Evidence (JSON)", readonly=True, copy=False,
        help="[{id,quote,supports}] verbatim SOP quotes grounding every value. "
             "The audit trail proving each artifact traces to the SOP.")
    question_spec_json = fields.Text(
        string="Question Spec (JSON)", readonly=True, copy=False,
        help="The answer-field contract the intake derived (answer_type, "
             "answer_fields, assets_per_question, solution_shape, uses).")
    sop_examples_json = fields.Text(
        string="SOP Examples (JSON)", readonly=True, copy=False,
        help="Examples mined from the SOP itself (worked example, sample "
             "response), the primary form/difficulty reference.")
    quality_criteria_json = fields.Text(
        string="Quality Criteria (JSON)", readonly=True, copy=False)
    failure_modes_json = fields.Text(
        string="Common Failure Modes (JSON)", readonly=True, copy=False)
    gaps_json = fields.Text(
        string="Gaps (JSON)", readonly=True, copy=False,
        help="Honesty log: rules that gave way on a thin SOP, silent fields.")
    conflicts_json = fields.Text(
        string="Conflicts (JSON)", readonly=True, copy=False,
        help="Recorded SOP-vs-example conflicts (never silently merged).")
    injection_flags_json = fields.Text(
        string="Injection Flags (JSON)", readonly=True, copy=False,
        help="SOP passages that tried to address the compiler as instructions.")
    similar_count = fields.Integer(
        string="Similar Generators", compute="_compute_similar_count",
        help="How many OTHER generators share enough weighted tags with this "
             "one to count as similar (shared-tag weight over the configured "
             "threshold). Drives the 'Similar' smart button.")
    # sanitize=False keeps the width:NN% inline style; every dynamic string in
    # _compute_similar_html MUST stay escape()d.
    similar_html = fields.Html(
        string="Similar Projects", sanitize=False, readonly=True,
        compute="_compute_similar_html",
        help="A ranked, presentation-only view of _similar_prompts(): how "
             "closely this SOP aligns with previous generators by weighted "
             "tag overlap, with an alignment % bar and the shared tags.")
    # Do not remove: dropping the field drops its DB column (user data).
    sample_questions = fields.Text(
        string="Sample Questions (legacy text)",
        help="Deprecated: replaced by the Sample Questions file upload.")
    sample_questions_file = fields.Binary(
        string="Sample Questions File", attachment=True,
        help="Upload a sample-questions file (PDF/DOCX/MD/image) to match the "
             "format - optional. Sent natively to the model so images inside the "
             "file are read too. Leave empty to follow the format inside the SOP.")
    sample_questions_filename = fields.Char(string="Sample Questions Filename")
    sop_question_count = fields.Integer(
        string="Questions to Generate", default=0,
        help="0 = let the model decide from the SOP; otherwise a target count.")
    question_count_mode = fields.Selection(
        [("auto", "Auto - model decides"), ("fixed", "Set a number")],
        string="Questions to generate",
        compute="_compute_question_count_mode",
        inverse="_inverse_question_count_mode")
    # Do not remove: dropping the field drops its DB column (user data), and the
    # 19.0.1.103.0 post-migrate reads it to seed allowed_question_type_ids.
    force_question_type = fields.Selection(
        QUESTION_TYPE_SELECTION, string="Force Question Type (legacy)",
        help="Deprecated: replaced by the Generate Only These Types allow-list.")
    allowed_question_type_ids = fields.Many2many(
        "etp.assessment.pro.question.type",
        relation="etp_pro_prompt_allowed_qtype_rel",
        column1="prompt_id", column2="qtype_id",
        string="Generate only these types",
        help="Allow-list: the generator may author ONLY these question types, "
             "choosing the best fit per item. Leave empty to let the model "
             "choose freely per the SOP.")
    select_all_types = fields.Boolean(
        string="Select all types", copy=False,
        help="Tick to load every question type at once; it resets immediately.")
    types_all_selected = fields.Boolean(compute="_compute_types_all_selected")
    quick_upload_file = fields.Binary(string="Upload SOP / Doc")
    quick_upload_filename = fields.Char()
    upload_sop_file = fields.Binary(string="Upload SOP")
    upload_sop_filename = fields.Char()
    upload_vendor_file = fields.Binary(string="Upload Vendor Doc")
    upload_vendor_filename = fields.Char()
    upload_client_file = fields.Binary(string="Upload Client Doc")
    upload_client_filename = fields.Char()
    upload_reference_file = fields.Binary(string="Upload Reference")
    upload_reference_filename = fields.Char()
    upload_sample_file = fields.Binary(string="Upload Sample Questions")
    upload_sample_filename = fields.Char()
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

    @api.onchange("upload_reference_file")
    def _onchange_upload_reference(self):
        self._add_resource(
            self.upload_reference_file, self.upload_reference_filename, "reference",
        )
        self.upload_reference_file = False
        self.upload_reference_filename = False

    @api.onchange("upload_sample_file")
    def _onchange_upload_sample(self):
        self._add_resource(
            self.upload_sample_file, self.upload_sample_filename, "sample",
        )
        self.upload_sample_file = False
        self.upload_sample_filename = False

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

    def _allowed_question_types(self):
        self.ensure_one()
        # Sort explicitly: an M2M written in the same transaction reads back in
        # link order, which would make the generation directive unstable.
        codes = list(dict.fromkeys(
            self.allowed_question_type_ids
                .sorted(lambda t: (t.sequence, t.id)).mapped("code")))
        if not codes and self.force_question_type:
            codes = [self.force_question_type]
        return tuple(codes)

    def _raise_count_to_type_floor(self):
        self.ensure_one()
        n = len(self.allowed_question_type_ids)
        if self.sop_question_count and self.sop_question_count < n:
            self.sop_question_count = n

    @api.onchange("allowed_question_type_ids", "sop_question_count")
    def _onchange_min_questions_for_types(self):
        self._raise_count_to_type_floor()

    @api.onchange("select_all_types")
    def _onchange_select_all_types(self):
        if self.select_all_types:
            self.allowed_question_type_ids = self.env[
                "etp.assessment.pro.question.type"].search([])
            self.select_all_types = False
            self._raise_count_to_type_floor()

    @api.depends("resource_ids", "resource_ids.category")
    def _compute_sop_resource_ids(self):
        for rec in self:
            rec.sop_resource_ids = rec.resource_ids.filtered(
                lambda r: r.category == "sop")

    @api.depends("allowed_question_type_ids")
    def _compute_types_all_selected(self):
        total = self.env["etp.assessment.pro.question.type"].search_count([])
        for rec in self:
            rec.types_all_selected = bool(
                total and len(rec.allowed_question_type_ids) >= total)

    @api.depends("sop_question_count")
    def _compute_question_count_mode(self):
        for rec in self:
            rec.question_count_mode = "fixed" if rec.sop_question_count else "auto"

    def _inverse_question_count_mode(self):
        for rec in self:
            rec._apply_question_count_mode()

    @api.onchange("question_count_mode")
    def _onchange_question_count_mode(self):
        # The inverse only fires on write(), never on in-form NewId assignment.
        self._apply_question_count_mode()

    def _apply_question_count_mode(self):
        for rec in self:
            if rec.question_count_mode == "auto":
                rec.sop_question_count = 0
            elif not rec.sop_question_count:
                rec.sop_question_count = len(rec.allowed_question_type_ids) or 5

    @api.constrains("sop_question_count", "allowed_question_type_ids")
    def _check_question_count(self):
        for rec in self:
            if rec.sop_question_count < 0:
                raise ValidationError(
                    "Questions to Generate cannot be negative.")
            n = len(rec.allowed_question_type_ids)
            if rec.sop_question_count and rec.sop_question_count < n:
                raise ValidationError(
                    "Questions to Generate (%d) is less than the %d selected "
                    "question types. Set it to at least %d, remove some types, "
                    "or set it to 0 to let the model decide the count."
                    % (rec.sop_question_count, n, n))

    def action_generate_from_sop(self):
        self.ensure_one()
        if not self.resource_ids and not (self.source_text or "").strip():
            raise UserError(
                "Upload a SOP document (or add notes) before generating.")
        self.write({"sop_gen_state": "queued", "sop_gen_error": False,
                    "state": "generating"})
        if self.question_count_mode == "fixed" and self.sop_question_count:
            count_txt = "%d question(s)" % self.sop_question_count
        else:
            count_txt = "auto (model decides)"
        types_txt = ", ".join(
            self.allowed_question_type_ids.mapped("name")) or "any (model picks)"
        self._log_activity(Markup(
            "<b>Generate Questions</b> requested.<br/>"
            "Count: %s<br/>Allowed types: %s") % (count_txt, types_txt))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Question Generation Queued",
                "message": "Reading your SOP in the background - draft questions "
                           "appear here within a minute or two; refresh to see "
                           "them.",
                "type": "success", "sticky": False,
            },
        }

    @api.model
    def _cron_generate_from_sop(self):
        """Must commit before the slow Vertex call: managed Postgres reaps the
        idle-in-transaction connection ('cursor already closed')."""
        from ..services import vertex
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_QUESTION_GEN,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            prompts = self.search(
                [("sop_gen_state", "in", ("queued", "generating"))], limit=2)
            if not prompts:
                return
            _logger.info(
                "etp_assessment SOP gen cron: %d prompt(s) to generate",
                len(prompts))
            for prompt in prompts:
                prompt.write({"sop_gen_state": "generating",
                              "sop_gen_error": False})
                self.env.cr.commit()
                try:
                    draft_ids = vertex.generate_questions_from_sop(
                        self.env, prompt,
                        count=prompt.sop_question_count or 0,
                        allowed_types=prompt._allowed_question_types())
                    # Commit the drafts before the contended state write, so a
                    # serialization retry there cannot lose them or re-run Vertex.
                    self.env.cr.commit()
                    # H-8: flip to 'finalizing' and COMMIT before the final
                    # status write. The drainer only selects queued/generating,
                    # so if the worker is killed between the draft commit and the
                    # 'done' write, the row is left 'finalizing' (needs an admin
                    # nudge) and is NEVER re-sent to Vertex — no duplicate drafts,
                    # no double spend.
                    prompt.write({"sop_gen_state": "finalizing"})
                    self.env.cr.commit()
                    self._finalize_sop_gen_state(prompt, len(draft_ids))
                    prompt._log_activity(Markup(
                        "<b>Generation complete</b> - %d draft question(s) "
                        "authored from the SOP and ready for review.")
                        % len(draft_ids))
                    self.env.cr.commit()
                    _logger.info(
                        "etp_assessment SOP gen cron: prompt %s -> %d draft(s)",
                        prompt.id, len(draft_ids))
                except vertex.VertexQuotaError:
                    self.env.cr.rollback()
                    _logger.warning(
                        "SOP generation for prompt %s hit Vertex quota (429); "
                        "re-queued for next tick.", prompt.id)
                    prompt.write({"sop_gen_state": "queued",
                                  "sop_gen_error": False})
                    self.env.cr.commit()
                except Exception as exc:  # noqa: BLE001 - isolate per prompt
                    self.env.cr.rollback()
                    _logger.exception(
                        "SOP generation failed for prompt %s", prompt.id)
                    prompt.write({"sop_gen_state": "failed",
                                  "sop_gen_error": str(exc)[:300]})
                    self.env.cr.commit()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_QUESTION_GEN,))

    def _finalize_sop_gen_state(self, prompt, draft_count):
        """Retry on 40001 only: the tag-extract cron writes the same row under a
        different advisory lock. Never re-queue here — the drainer cannot tell
        'needs generation' from 'needs finalization', so it would re-run Vertex
        and duplicate the already-committed drafts."""
        import time
        import psycopg2
        from psycopg2 import errorcodes, errors as pg_errors
        vals = {
            "state": "done",
            "sop_gen_state": "done",
            "last_extract_summary": "Generated %s draft(s) from SOP" % draft_count,
        }
        for attempt in range(_SOP_GEN_FINALIZE_MAX_ATTEMPTS):
            try:
                prompt.write(vals)
                self.env.cr.commit()
                return
            except psycopg2.OperationalError as exc:
                is_serialization = (
                    isinstance(exc, pg_errors.SerializationFailure)
                    or getattr(exc, "pgcode", None)
                    == errorcodes.SERIALIZATION_FAILURE)
                if (not is_serialization
                        or attempt == _SOP_GEN_FINALIZE_MAX_ATTEMPTS - 1):
                    raise
                self.env.cr.rollback()
                prompt.invalidate_recordset()
                _logger.warning(
                    "SOP gen finalize hit a serialization race on prompt %s "
                    "(attempt %s/%s); rolled back the state write and retrying "
                    "with the drafts already committed.",
                    prompt.id, attempt + 1, _SOP_GEN_FINALIZE_MAX_ATTEMPTS)
                time.sleep(0.1 * (attempt + 1))

    def _finalize_tag_extract_state(self, prompt, tags, raw):
        """Retry on 40001 only: the SOP-gen cron writes the same row under a
        different advisory lock. Mirrors _finalize_sop_gen_state."""
        import time
        import psycopg2
        from psycopg2 import errorcodes, errors as pg_errors
        vals = {
            "tag_ids": [(6, 0, tags.ids)],
            "tags_json": raw or False,
            "tag_extract_state": "done",
        }
        for attempt in range(_TAG_EXTRACT_FINALIZE_MAX_ATTEMPTS):
            try:
                prompt.write(vals)
                self.env.cr.commit()
                return
            except psycopg2.OperationalError as exc:
                is_serialization = (
                    isinstance(exc, pg_errors.SerializationFailure)
                    or getattr(exc, "pgcode", None)
                    == errorcodes.SERIALIZATION_FAILURE)
                if (not is_serialization
                        or attempt == _TAG_EXTRACT_FINALIZE_MAX_ATTEMPTS - 1):
                    raise
                self.env.cr.rollback()
                prompt.invalidate_recordset()
                _logger.warning(
                    "Tag extract finalize hit a serialization race on prompt %s "
                    "(attempt %s/%s); rolled back the state write and retrying "
                    "with the tags already committed.",
                    prompt.id, attempt + 1, _TAG_EXTRACT_FINALIZE_MAX_ATTEMPTS)
                time.sleep(0.1 * (attempt + 1))

    def _run_tag_extract_inline(self):
        self.ensure_one()
        from ..services import vertex
        self.write({"tag_extract_state": "generating", "tag_extract_error": False})
        try:
            names, raw = vertex.extract_tags_from_sop(self.env, self)
        except vertex.VertexQuotaError as exc:
            self.write({"tag_extract_state": "failed",
                        "tag_extract_error": "Vertex quota hit (429); retry shortly."})
            raise UserError(
                "Tag extraction hit the Vertex rate limit (429). Please try "
                "again in a moment.") from exc
        except Exception as exc:  # noqa: BLE001 - record terminal state, surface it
            self.write({"tag_extract_state": "failed",
                        "tag_extract_error": str(exc)[:300]})
            raise UserError("Tag extraction failed: %s" % exc) from exc
        tags = self.env["etp.assessment.pro.tag"]._get_or_create(names)
        self.write({
            "tag_ids": [(6, 0, tags.ids)],
            "tags_json": raw or False,
            "tag_extract_state": "done",
        })
        return tags

    def action_approve_all_drafts(self):
        self.ensure_one()
        drafts = self.question_ids.filtered(lambda r: r.state == "draft")
        if not drafts:
            raise UserError("There are no pending drafts to approve.")
        # action_approve logs the approval rollup to this generator's chatter,
        # so no separate note here (that would double-log the same event).
        drafts.action_approve()
        return True

    def action_extract_tags(self):
        self.ensure_one()
        if not self.resource_ids and not (self.source_text or "").strip():
            raise UserError(
                "Upload a SOP document (or add notes) before extracting tags.")
        tags = self._run_tag_extract_inline()
        self._log_activity(Markup(
            "<b>Extract Tags</b> run - %d semantic tag(s) extracted from the "
            "SOP.") % len(tags))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tags Extracted",
                "message": "%d semantic tag(s) extracted from the SOP."
                           % len(tags),
                "type": "success", "sticky": False,
            },
        }

    @api.model
    def _cron_extract_tags(self):
        """Must commit before the slow Vertex call ('cursor already closed').
        Mirrors _cron_generate_from_sop."""
        from ..services import vertex
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_TAG_EXTRACT,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            prompts = self.search(
                [("tag_extract_state", "in", ("queued", "generating"))], limit=2)
            if not prompts:
                return
            _logger.info(
                "etp_assessment tag extract cron: %d prompt(s) to tag",
                len(prompts))
            for prompt in prompts:
                prompt.write({"tag_extract_state": "generating",
                              "tag_extract_error": False})
                self.env.cr.commit()
                try:
                    names, raw = vertex.extract_tags_from_sop(self.env, prompt)
                    tags = self.env["etp.assessment.pro.tag"]._get_or_create(
                        names)
                    # Commit the tags before the contended state write, so a
                    # serialization retry there cannot lose them or re-run Vertex.
                    self.env.cr.commit()
                    self._finalize_tag_extract_state(prompt, tags, raw)
                    _logger.info(
                        "etp_assessment tag extract cron: prompt %s -> %d tag(s)",
                        prompt.id, len(tags))
                except vertex.VertexQuotaError:
                    self.env.cr.rollback()
                    _logger.warning(
                        "Tag extraction for prompt %s hit Vertex quota (429); "
                        "re-queued for next tick.", prompt.id)
                    prompt.write({"tag_extract_state": "queued",
                                  "tag_extract_error": False})
                    self.env.cr.commit()
                except Exception as exc:  # noqa: BLE001 - isolate per prompt
                    self.env.cr.rollback()
                    _logger.exception(
                        "Tag extraction failed for prompt %s", prompt.id)
                    prompt.write({"tag_extract_state": "failed",
                                  "tag_extract_error": str(exc)[:300]})
                    self.env.cr.commit()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_TAG_EXTRACT,))

    def _tag_prefix_weight(self, prefix):
        param = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.tag_weight_%s" % (prefix or ""))
        if param:
            try:
                return float(param)
            except (TypeError, ValueError):
                pass
        return TAG_PREFIX_WEIGHTS.get(
            prefix or "", TAG_DEFAULT_PREFIX_WEIGHT)

    def _tag_similar_min_score(self):
        param = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.tag_similar_min_score")
        if param:
            try:
                return float(param)
            except (TypeError, ValueError):
                pass
        return TAG_SIMILAR_MIN_SCORE_DEFAULT

    def _similar_prompts(self, limit=5, min_score=None):
        self.ensure_one()
        my_tags = self.tag_ids
        if not my_tags:
            return []
        # psycopg2 cannot adapt a NewId into the raw self-join below; fall back
        # to the persisted origin, and bail when there is no DB row yet.
        rec_id = self.id if isinstance(self.id, int) else self._origin.id
        if not isinstance(rec_id, int) or not rec_id:
            return []
        field = self._fields["tag_ids"]
        rel, col_prompt, col_tag = (
            field.relation, field.column1, field.column2)
        self.env.cr.execute(
            """
            SELECT other.{col_prompt} AS other_id,
                   array_agg(other.{col_tag}) AS shared_ids
              FROM {rel} mine
              JOIN {rel} other
                ON other.{col_tag} = mine.{col_tag}
               AND other.{col_prompt} != mine.{col_prompt}
             WHERE mine.{col_prompt} = %s
             GROUP BY other.{col_prompt}
            """.format(rel=rel, col_prompt=col_prompt, col_tag=col_tag),
            (rec_id,),
        )
        rows = self.env.cr.fetchall()
        if not rows:
            return []
        weight_by_prefix = {}

        def weight(tag):
            prefix = tag.prefix or ""
            if prefix not in weight_by_prefix:
                weight_by_prefix[prefix] = self._tag_prefix_weight(prefix)
            return weight_by_prefix[prefix]

        my_weight = sum(weight(t) for t in my_tags)
        other_prompts = self.browse([r[0] for r in rows])
        other_prompts.tag_ids  # prefetch tag sets in one read
        Tag = self.env["etp.assessment.pro.tag"]
        results = []
        for other, (_other_id, shared_ids) in zip(other_prompts, rows):
            shared = Tag.browse([tid for tid in shared_ids if tid])
            shared_weight = sum(weight(t) for t in shared)
            union_weight = (
                my_weight + sum(weight(t) for t in other.tag_ids)
                - shared_weight)
            score = shared_weight / union_weight if union_weight else 0.0
            if min_score is not None and score < min_score:
                continue
            results.append({
                "prompt": other,
                "score": score,
                "shared": shared,
                "shared_weight": shared_weight,
            })
        results.sort(key=lambda d: (d["score"], d["shared_weight"]),
                     reverse=True)
        return results[:limit] if limit else results

    @api.depends("tag_ids")
    def _compute_similar_count(self):
        threshold = self._tag_similar_min_score()
        for rec in self:
            if not rec.tag_ids:
                rec.similar_count = 0
                continue
            rec.similar_count = sum(
                1 for sim in rec._similar_prompts(limit=None)
                if sim["shared_weight"] >= threshold)

    @staticmethod
    def _simrank_prefix_class(prefix):
        known = {"task", "domain", "skill", "modality", "output-format"}
        p = (prefix or "").strip().lower()
        return "etp-simrank-tag--%s" % p if p in known else ""

    def _simrank_empty(self, message):
        return Markup(
            '<div class="etp-simrank etp-simrank-empty">'
            '<i class="fa fa-project-diagram"></i>'
            '<span>{msg}</span></div>'
        ).format(msg=escape(message))

    @api.depends("tag_ids")
    def _compute_similar_html(self):
        for rec in self:
            if not rec.id or not rec.tag_ids:
                rec.similar_html = rec._simrank_empty(
                    "Add or extract SOP tags to see how this project aligns "
                    "with previous ones.")
                continue
            matches = rec._similar_prompts(limit=8)
            if not matches:
                rec.similar_html = rec._simrank_empty(
                    "No aligned projects yet - no other generator shares "
                    "these SOP tags.")
                continue
            rows = Markup("")
            for i, match in enumerate(matches):
                other = match["prompt"]
                pct = max(0, min(100, round((match["score"] or 0.0) * 100)))
                rank = i + 1
                rank_cls = " etp-simrank-rank--top" if rank <= 3 else ""
                pills = Markup("")
                for tag in match["shared"]:
                    pills += Markup(
                        '<span class="etp-simrank-tag {cls}">{label}</span>'
                    ).format(
                        cls=rec._simrank_prefix_class(tag.prefix),
                        label=escape(tag.label or tag.name or ""))
                rows += Markup(
                    '<div class="etp-simrank-row">'
                    '<span class="etp-simrank-rank{rank_cls}">{rank}</span>'
                    '<div class="etp-simrank-main">'
                    '<div class="etp-simrank-top">'
                    '<a class="etp-simrank-name" href="{href}">{name}</a>'
                    '<span class="etp-simrank-pct">{pct}%</span>'
                    '</div>'
                    '<div class="etp-simrank-bar">'
                    '<span class="etp-simrank-fill" style="width:{pct}%"></span>'
                    '</div>'
                    '<div class="etp-simrank-tags">{pills}</div>'
                    '</div></div>'
                ).format(
                    rank_cls=rank_cls, rank=rank,
                    href=escape(
                        "/web#id=%d&model=etp.assessment.pro.prompt"
                        "&view_type=form" % other.id),
                    name=escape(other.name or "Untitled"),
                    pct=pct, pills=pills)
            rec.similar_html = Markup(
                '<div class="etp-simrank">{}</div>').format(rows)

    def action_view_similar(self):
        self.ensure_one()
        threshold = self._tag_similar_min_score()
        ids = [sim["prompt"].id
               for sim in self._similar_prompts(limit=None)
               if sim["shared_weight"] >= threshold]
        return {
            "type": "ir.actions.act_window",
            "name": "Generators Similar to %s" % (self.name or ""),
            "res_model": "etp.assessment.pro.prompt",
            "domain": [("id", "in", ids)],
            "view_mode": "list,form",
            "views": [
                (self.env.ref(
                    "etp_assessment_pro."
                    "etp_assessment_pro_prompt_similar_tree").id, "list"),
                (False, "form"),
            ],
            "target": "current",
            "help": (
                "<p class='o_view_nocontent_smiling_face'>No similar generators"
                "</p><p>Similarity ranks other generators by the weight of the "
                "SOP tags they share with this one.</p>"),
        }

    def action_normalize_tags(self):
        """Fix the tag vocabulary AND evolve the shared knowledge base.

        Two stages, so the knowledge base gets BETTER every time a project is
        added and this is run:
          1. CONSOLIDATE the whole live vocabulary (LLM clusters true synonyms,
             merges drift onto one canonical key, and refreshes every readable
             display) — data-driven, no hardcoded synonym map.
          2. RE-EXTRACT the selected generators against that cleaned vocabulary,
             so their tags + knowledge profile snap onto the shared canonical
             values. Cleaner shared vocabulary -> the NEXT project's extraction
             converges on it too -> similarity ranking and future assessments
             keep improving as projects accumulate.
        """
        from ..services import vertex
        # Stage 1: consolidate the shared vocabulary (once, globally).
        consolidated = {}
        try:
            consolidated = vertex.consolidate_vocabulary(self.env)
        except vertex.VertexQuotaError:
            consolidated = {"error": "Vertex rate limit (429); tags re-extracted "
                                     "without a consolidation pass this time."}
        except Exception:  # noqa: BLE001 - consolidation is best-effort
            _logger.exception("Vocabulary consolidation failed")
            consolidated = {"error": "consolidation step failed (see logs)"}

        # Stage 2: re-extract the selected generators onto the cleaned vocabulary.
        targets = self.filtered(
            lambda p: p.resource_ids.filtered(lambda r: r.category == "sop")
            or (p.source_text or "").strip())
        skipped = len(self) - len(targets)
        done = 0
        failed = 0
        for prompt in targets:
            try:
                prompt._run_tag_extract_inline()
                prompt._log_activity(Markup(
                    "<b>Normalize Tags</b> - tags re-extracted onto the cleaned "
                    "shared vocabulary."))
                done += 1
            except UserError:
                failed += 1
                _logger.exception(
                    "Tag normalize failed for generator %s", prompt.id)

        parts = []
        if consolidated.get("merged_groups") or consolidated.get("displays_updated"):
            parts.append(
                "Vocabulary tidied: %d duplicate group(s) merged, %d tag(s) "
                "absorbed, %d readable name(s) refreshed."
                % (consolidated.get("merged_groups", 0),
                   consolidated.get("tags_absorbed", 0),
                   consolidated.get("displays_updated", 0)))
        elif consolidated.get("error"):
            parts.append(consolidated["error"])
        parts.append("Re-extracted tags for %d of %d selected generator(s)."
                     % (done, len(targets)))
        if failed:
            parts.append("%d failed (see the generator's Tag Extraction error)."
                         % failed)
        if skipped:
            parts.append("%d skipped (no SOP document)." % skipped)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tags Normalized & Knowledge Base Refreshed",
                "message": " ".join(parts),
                "type": "warning" if failed else "success",
                "sticky": bool(failed),
            },
        }

    @api.model
    def action_backfill_all_tags(self):
        prompts = self.search([
            ("tag_extract_state", "in", ("idle", "failed")),
            ("tag_ids", "=", False),
            ("resource_ids.category", "=", "sop"),
        ])
        done = 0
        for prompt in prompts:
            try:
                prompt._run_tag_extract_inline()
                done += 1
            except UserError:
                _logger.exception(
                    "Tag backfill failed for generator %s", prompt.id)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tag Backfill Complete",
                "message": "Extracted tags for %d of %d generator(s)."
                           % (done, len(prompts)),
                "type": "success", "sticky": False,
            },
        }


class EtpAssessmentPromptQuestion(models.Model):
    _name = "etp.assessment.pro.prompt.question"
    _description = "Prompt Draft Question"
    _order = "id"

    prompt_id = fields.Many2one(
        "etp.assessment.pro.prompt", required=True, ondelete="cascade"
    )
    name = fields.Char(string="Title", required=True)
    question_prompt = fields.Text(string="Question Prompt")
    description = fields.Text(
        string="Description",
        help="Optional candidate-facing description. NEVER put options or the "
             "correct answer here - those live in dimensions / the answer key.")
    time_minutes = fields.Integer(string="Time (minutes)", default=0)
    question_type = fields.Selection(
        QUESTION_TYPE_SELECTION,
        default="mcq",
    )
    medium_display = fields.Char(
        string="Medium", compute="_compute_medium_display",
        help="Image for the image question types (A/B Evaluation, "
             "Prompt/Labelling) - the only ones that render a picture; Text "
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
    detection_mode = fields.Selection(
        DETECTION_MODE_SELECTION,
        string="Detection Mode",
        default="object",
        help="image_label: photo (detect objects) vs UI screenshot (detect "
             "clickable UI elements). Copied onto the bank question on approve "
             "so detection uses the right prompt.")
    options_json = fields.Text(string="Options (JSON)")
    correct_answer_json = fields.Text(string="Correct Answer (JSON)")
    # --- Generation-run tracking (UI clarity: "which questions came from which
    # run I just triggered"). gen_batch is stamped at create time by the SOP
    # generation cron; role is derived from the "Task:" title prefix so the
    # real-task replicas are self-labelling in every list. ---
    gen_batch = fields.Char(
        string="Generation Run", readonly=True, copy=False, index=True,
        help="Timestamped id of the generation run that produced this draft, so "
             "you can see at a glance which batch each question came from.")
    gen_batch_label = fields.Char(
        string="Run", compute="_compute_gen_batch_label", store=True,
        help="Human-friendly label for the generation run (time of the run).")
    solution_json = fields.Text(
        string="Solution / Golden Answer (JSON)", readonly=True, copy=False,
        help="The most correct answer in an ideal worker's voice (research "
             "solutions.answers), stored as historic ground truth and fed to "
             "the subjective judge at score time so it decomposes golden claims "
             "before reading the worker answer.")
    solution_rationale = fields.Text(
        string="Solution Rationale", readonly=True, copy=False,
        help="How the golden answer is known (construction ground truth, the "
             "SOP's own rule, or derivation). Provenance for the answer key.")
    covers_elements_json = fields.Text(
        string="Covers Elements (JSON)",
        help="Required-element ids (from the SOP metadata) this question's "
             "scenario uniquely exercises. Research schema 1.5 coverage; "
             "threaded to scoring so SOP-Coverage grades against real elements.")
    dimensions_json = fields.Text(
        string="Dimensions (JSON)",
        help="General multi-dimension answer key: a JSON list of "
             '{"label", "options":[..], "correct":[..]} objects. Used by '
             "image_ab (and any multi-objective question). When present it "
             "supersedes options_json/correct_answer_json. ``correct`` entries "
             "may be option strings OR 0-based indices into ``options``.")
    rubric_json = fields.Text(string="Rubric (JSON)")
    behavioural_key_json = fields.Text(
        string="Behavioural Key (JSON)", copy=False,
        help="image_label DENSE answer key: a JSON list of {number, element, "
             "functionality} grading the ACTION each numbered box performs. "
             "Copied onto the 'single' bank image on approve so the candidate UI "
             "and scoring treat model-authored boxes like DOM-captured ones.")
    label_boxes_json = fields.Text(
        string="Label Boxes (JSON)", copy=False,
        help="image_label DENSE box geometry: a JSON list of {number, box_2d, "
             "label, description} in the 0-1000 grid. Used at approve to draw the "
             "numbered overlay on the rendered screenshot via annotate_image.")
    defect_plan_json = fields.Text(
        string="Defect Plan (JSON)", copy=False,
        help="image_label DEFECT form (q7r): a JSON object {base_prompt, "
             "defects:[{marker, kind, op, spec, marker_xy, flaw}]}. The pipeline "
             "renders a CLEAN base image from base_prompt, then PLANTS each defect "
             "deterministically with PIL at a known pixel and stamps the numbered "
             "marker on the ACTUAL drawn region (defect_render.plant) so the answer "
             "key is true by construction and no marker lands on empty space.")
    coverage_expected = fields.Selection(
        [("yes", "Yes"), ("no", "No")],
        string="Coverage Expected", copy=False,
        help="image_label DENSE coverage gate ground truth: 'no' when the brief "
             "deliberately leaves one interactive element un-boxed, else 'yes'. "
             "Copied onto the bank image on approve.")
    omitted_element_json = fields.Text(
        string="Omitted Element (JSON)", copy=False,
        help="image_label DENSE: the deliberately un-boxed element backing a "
             "coverage:No gate, copied onto the bank image on approve.")
    label_application = fields.Char(
        string="Label Application", copy=False,
        help="image_label DENSE: the app/site the screenshot depicts, copied "
             "onto the bank image on approve and graded as one identification "
             "checklist point.")
    source_url = fields.Char(
        string="Source URL", copy=False,
        help="image_label REAL-PAGE CAPTURE (preferred): a stable public web "
             "page URL. Copied onto the 'single' bank image on approve so the "
             "detect cron captures the live DOM (numbered boxes at real element "
             "geometry) instead of labelling a synthetic render; the synthetic "
             "brief is kept as the hybrid fallback.")
    capture_config_json = fields.Text(
        string="Capture Config (JSON)", copy=False,
        help='image_label capture directives {"viewport","wait_ms","dismiss"} '
             "copied onto the bank image on approve and threaded into the live "
             "capture (settle delay + cookie/consent dismissal).")
    omit_spec_json = fields.Text(
        string="Omit Spec (JSON)", copy=False,
        help="image_label capture directive that leaves ONE interactive element "
             'deliberately unboxed so the coverage answer is "No" by '
             "construction; copied onto the bank image on approve.")
    official_reasoning = fields.Text(
        string="Official Reasoning",
        help="image_ab: the official rationale the LLM grades the candidate's "
             "justification against.")
    flaw_plan_json = fields.Text(
        string="Flaw Plan (JSON)",
        help="image_ab flaw-injection plan: {faithful_side, worker_prompt, "
             "render_prompts:{a,b}, planted:{a,b}, construction_keys} (the older "
             "flawed_side/clean_prompt/flawed_prompt/injected_flaws shape is also "
             "accepted). The answer key is DERIVED from construction_keys "
             "(ground-truth by construction). "
             "Surfaced read-only so a reviewer can eyeball the FLAWED image "
             "against injected_flaws before approving; copied to the bank "
             "question on approve, where a key-drift guard hard-fails on any "
             "mismatch. Empty for non-flaw / pre-Phase-3 drafts (guards no-op).")
    verification_json = fields.Text(
        string="Flaw Verification (JSON)", copy=False,
        help="Phase-3 render QA for image_ab: per-side record of whether each "
             "planted flaw was VISIBLY confirmed in the rendered image and how "
             "many times that side was re-rendered. needs_review=true means a "
             "planted flaw never rendered after the bounded retries, so the "
             "construction key it backs is not justified by the pixels and "
             "approval is blocked until it is fixed. Empty when verification is "
             "disabled, unavailable (Vertex down / no creds), or the draft has "
             "no image_ab flaw plan.")
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
    video_brief_json = fields.Text(
        string="Video Briefs (JSON)",
        help='video_prompt twin of image_brief_json: a JSON list of '
             '{"slot","label","prompt"} clip briefs (reference + output, or a '
             "single clip) authored by the text model. The clips are uploaded by "
             "an admin (Phase 1) or generated by Veo (Phase 3); nothing renders "
             "from these at generation time.")
    video_state = fields.Selection(
        [
            ("none", "No Video Needed"),
            ("pending", "Video Pending"),
            ("generating", "Generating"),
            ("rendered", "Video Rendered"),
            ("failed", "Generation Failed"),
        ],
        default="none", string="Video State", copy=False,
        help="video_prompt async Veo lifecycle: pending = briefs exist, waiting "
             "to submit (or Veo not configured, so the admin uploads clips); "
             "generating = Veo ops submitted, polling; rendered = every clip is "
             "back and staged; failed = an op failed past the attempt cap. The "
             "config gate keeps it 'pending' when Veo/creds are absent so the "
             "Phase-1 upload path fully works.")
    video_op_json = fields.Text(
        string="Video Op State (JSON)", copy=False,
        help="Per-slot Veo long-running op state: {slot: {op_name, state, "
             "attempts, label}}. Persisted before video_state flips to "
             "'generating' so a killed worker resumes without double-submitting "
             "an op (idempotency handle).")
    video_files_json = fields.Text(
        string="Video Files (JSON)", copy=False,
        help='video twin of images_json: a JSON list of {"slot","label","url"|'
             '"data"} clips staged by the poll cron, materialized to '
             "question.video rows on approve.")
    video_error = fields.Char(
        string="Video Error", copy=False,
        help="The error that failed the last Veo generation attempt, surfaced "
             "so a reviewer can fix the brief or fall back to upload.")
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
    image_render_attempts = fields.Integer(
        default=0, copy=False,
        help="How many times the render cron has tried this draft. A 429 quota "
             "hit does NOT count; only genuine partial/failed renders do, so a "
             "draft flips to 'failed' after the cap instead of retrying forever.")
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
    answer_dimension_ids = fields.One2many(
        "etp.assessment.pro.prompt.question.dimension", "draft_id",
        string="Answer Key")
    ak_ideal_answer = fields.Text(
        string="Ideal Answer", compute="_compute_answer_key_fields",
        inverse="_inverse_answer_key_fields",
        help="image_prompt/image_label: the ideal prompt or labels the candidate "
             "is graded against.")
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
    dimensions_preview = fields.Html(
        string="Answer Key Preview", compute="_compute_previews",
        sanitize=False)
    image_preview = fields.Html(
        string="Images", compute="_compute_previews", sanitize=False)
    has_images = fields.Boolean(compute="_compute_previews")
    has_dimensions = fields.Boolean(compute="_compute_previews")
    video_preview = fields.Html(
        string="Video Clips", compute="_compute_video_preview", sanitize=False)
    has_video_clips = fields.Boolean(compute="_compute_video_preview")
    image_summary = fields.Char(
        string="Image Files", compute="_compute_image_summary",
        help="Human-readable summary of the stored images (slot, label, size). "
             "The raw base64 image data is never shown in the UI.")

    @api.depends("question_type")
    def _compute_medium_display(self):
        for rec in self:
            rec.medium_display = (
                "Image" if rec.question_type in IMAGE_QUESTION_TYPES else "Text")

    @api.depends("gen_batch", "create_date")
    def _compute_gen_batch_label(self):
        """Human label for the generation run. Falls back to the create date so
        older drafts (no gen_batch) still group sensibly."""
        for rec in self:
            if rec.gen_batch:
                # gen_batch is 'YYYYmmdd-HHMMSS'; render a friendly time
                raw = rec.gen_batch
                try:
                    from datetime import datetime
                    dt = datetime.strptime(raw[:15], "%Y%m%d-%H%M%S")
                    rec.gen_batch_label = "Run " + dt.strftime("%b %d, %H:%M")
                except Exception:  # noqa: BLE001
                    rec.gen_batch_label = "Run " + raw
            elif rec.create_date:
                rec.gen_batch_label = "Run " + rec.create_date.strftime(
                    "%b %d, %H:%M")
            else:
                rec.gen_batch_label = "Run"

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
                    src = (spec.get("annotated_url") or spec.get("annotated_data")
                           or spec.get("url") or spec.get("src")
                           or spec.get("data"))
                    if not src:
                        continue
                    # L-2: only render http(s), our own relative paths, or
                    # image data: URLs. Blocks javascript:/vbscript:/other
                    # schemes from an LLM/admin-supplied spec ever reaching an
                    # href-like attribute if this markup is refactored later.
                    if not _preview_src_ok(src):
                        continue
                    label = _html.escape(str(
                        spec.get("label") or spec.get("slot") or ""))
                    imgs.append(
                        '<figure style="display:inline-block;margin:6px;'
                        'text-align:center">'
                        '<span class="etp-image-zoomable" tabindex="0" '
                        'role="button" aria-label="Zoom image" '
                        'title="Click to zoom">'
                        f'<img src="{_html.escape(src)}" '
                        'style="max-height:160px;max-width:220px;'
                        'border:1px solid #dee2e6;border-radius:4px"/>'
                        '</span>'
                        f'<figcaption class="text-muted small">{label}'
                        '</figcaption></figure>')
            rec.image_preview = "".join(imgs) if imgs else False
            rec.has_images = bool(imgs)

    @api.depends("video_files_json", "question_type")
    def _compute_video_preview(self):
        import html as _html
        for rec in self:
            blocks = []
            if rec.question_type == "video_prompt" and isinstance(rec.id, int):
                for f in rec._video_files():
                    slot = f.get("slot") or "single"
                    if not (f.get("url") or f.get("data")):
                        continue
                    label = _html.escape(str(f.get("label") or slot))
                    src = "/etp_assessment/admin_draft_qvideo/%d/%s" % (
                        rec.id, _html.escape(str(slot)))
                    blocks.append(
                        '<figure style="display:inline-block;margin:6px;'
                        'text-align:center;vertical-align:top">'
                        '<video controls preload="metadata" '
                        'style="max-height:220px;max-width:320px;'
                        'border:1px solid #dee2e6;border-radius:4px" '
                        f'src="{src}"></video>'
                        f'<figcaption class="text-muted small">{label}'
                        '</figcaption></figure>')
            rec.video_preview = "".join(blocks) if blocks else False
            rec.has_video_clips = bool(blocks)

    _RUBRIC_STR_KEYS = (("ak_scoring_guide", "scoring_guide"),
                        ("ak_pass_condition", "pass_condition"))
    _RUBRIC_LIST_KEYS = (("ak_mandatory_elements", "mandatory_elements"),
                         ("ak_penalty_rules", "penalty_rules"),
                         ("ak_checklist", "checklist"),
                         ("ak_constraints", "constraints"))

    def _ideal_answer_key(self):
        self.ensure_one()
        if self.question_type == "image_prompt":
            return "ideal_prompt"
        if self.question_type == "image_label":
            return "ideal_labels"
        return "ideal_answer"

    @api.depends("rubric_json", "question_type")
    def _compute_answer_key_fields(self):
        import json as _json
        for rec in self:
            try:
                data = _json.loads(rec.rubric_json or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            rec.ak_ideal_answer = data.get(rec._ideal_answer_key()) or False
            for fname, key in rec._RUBRIC_STR_KEYS:
                rec[fname] = data.get(key) or False
            for fname, key in rec._RUBRIC_LIST_KEYS:
                val = data.get(key) or []
                if not isinstance(val, list):
                    val = [val]
                rec[fname] = "\n".join(str(x) for x in val) or False

    def _inverse_answer_key_fields(self):
        import json as _json
        for rec in self:
            try:
                data = _json.loads(rec.rubric_json or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
            ideal_key = rec._ideal_answer_key()
            for k in ("ideal_answer", "ideal_prompt", "ideal_labels"):
                if k != ideal_key:
                    data.pop(k, None)
            if (rec.ak_ideal_answer or "").strip():
                data[ideal_key] = rec.ak_ideal_answer
            else:
                data.pop(ideal_key, None)
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

    def action_approve(self):
        Question = self.env["etp.assessment.pro.question"]
        drafts = self.filtered(lambda r: r.state == "draft")
        if not self.env.context.get("skip_image_ready_guard"):
            not_ready = drafts.filtered(
                lambda r: r.question_type in IMAGE_QUESTION_TYPES
                and not r._current_images())
            if not_ready:
                raise UserError(
                    "These image questions have no image yet, so approving them "
                    "would publish a question with a missing picture. Wait for "
                    "rendering to finish (or upload an image), then approve:\n%s"
                    % "\n".join("- %s" % (r.name or r.question_prompt or "draft")
                                for r in not_ready))
        for rec in drafts:
            if rec.question_type == "image_ab" and rec.verification_json:
                rec._assert_flaw_render_verified()
        for rec in drafts:
            vals = {
                "name": rec.name,
                "prompt": rec.question_prompt or rec.name,
                "question_type": rec.question_type or "mcq",
                "generator_id": rec.prompt_id.id,
                "difficulty": rec.difficulty or False,
                "detection_mode": rec.detection_mode or "object",
                "time_minutes": rec.time_minutes or 0,
                "description": rec.description or False,
                "subjective_rubric_json": rec.rubric_json or False,
                "official_reasoning": rec.official_reasoning or False,
                "flaw_plan_json": rec.flaw_plan_json or False,
                "covers_elements_json": rec.covers_elements_json or False,
                "solution_json": rec.solution_json or False,
                "solution_rationale": rec.solution_rationale or False,
                "verification_json": rec.verification_json or False,
                "source_ref": "gen:%s" % rec.prompt_id.name,
            }
            q = Question.create(vals)
            if rec.question_type in ("mcq", "msq", "image_ab",
                                     "image_prompt", "image_label"):
                rec._materialize_dimensions(q)
            if rec.question_type == "image_ab" and rec.flaw_plan_json:
                rec._assert_no_key_drift(q)
            if rec.question_type in ("image_ab", "image_prompt", "image_label"):
                rec._materialize_images(q)
            if rec.question_type in VIDEO_QUESTION_TYPES:
                rec._materialize_videos(q)
            if rec.question_type == "image_label":
                rec._apply_authored_label_key(q)
            rec.write({"state": "approved", "approved_question_id": q.id})
        self._log_drafts_to_generators(drafts, "approved")
        return True

    def _log_drafts_to_generators(self, drafts, verb):
        """Roll a per-draft approve/deny up to each parent generator's chatter.

        Drafts approved/denied from the bank list can span several generators,
        so group by prompt_id and post one summary line per generator.
        """
        by_gen = {}
        for rec in drafts:
            if rec.prompt_id:
                by_gen.setdefault(rec.prompt_id, self.browse())
                by_gen[rec.prompt_id] |= rec
        for generator, recs in by_gen.items():
            titles = ", ".join(
                (r.name or r.question_prompt or "draft")[:60] for r in recs)
            generator._log_activity(Markup(
                "<b>%d draft(s) %s</b>: %s")
                % (len(recs), escape(verb), escape(titles)))

    def _assert_no_key_drift(self, bank_question):
        self.ensure_one()
        keys = ab_construction_keys(self.flaw_plan_json)
        if not keys:
            return
        materialized = {}
        for qd in bank_question.question_dimension_ids:
            code = ab_code_from_label(qd.name)
            if code:
                materialized[code] = [
                    ol.name for ol in qd.option_line_ids if ol.is_correct]
        drift = ab_key_drift(materialized, keys)
        if drift:
            raise UserError(
                "Key drift: the materialized answer key for %r does not match "
                "its flaw-injection construction keys, so it is no longer "
                "ground-truth - refusing to approve.\n%s"
                % (self.name or "draft", "\n".join(drift)))

    def _assert_flaw_render_verified(self):
        self.ensure_one()
        import json as _json
        try:
            rec = _json.loads(self.verification_json or "{}")
        except (ValueError, TypeError):
            return
        if not isinstance(rec, dict) or not rec.get("needs_review"):
            return
        unconfirmed = []
        for slot, side in (rec.get("sides") or {}).items():
            if not isinstance(side, dict) or side.get("confirmed") \
                    or side.get("unavailable"):
                continue
            for v in side.get("verdicts") or []:
                if isinstance(v, dict) and not v.get("present"):
                    unconfirmed.append("%s: %s" % (slot, v.get("flaw") or "?"))
        raise UserError(
            "Flaw verification failed: a planted flaw never rendered into the "
            "image after re-generation, so the construction key it backs is not "
            "justified by the pixels - refusing to approve %r. Regenerate the "
            "image or fix the flaw plan.\n%s"
            % (self.name or "draft", "\n".join(unconfirmed) or "(unconfirmed)"))

    def _dimension_specs(self):
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
        """String matching must stay ahead of index resolution: an option
        literally named "1" matches by value, not as index 1."""
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
            cs_stripped = cs.strip()
            if cs_stripped.lstrip("-").isdigit():
                idx = int(cs_stripped)
                if 0 <= idx < len(options):
                    out.append(options[idx])
        return out

    def _sync_answer_relational_from_json(self):
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
        self._sync_answer_relational_from_json()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec, vals in zip(records, vals_list):
            if rec.answer_dimension_ids:
                continue
            if (vals.get("dimensions_json") or vals.get("options_json")
                    or vals.get("correct_answer_json")):
                rec._sync_answer_relational_from_json()
        return records

    def _materialize_dimensions(self, bank_question):
        """Always create a PRIVATE dimension per question; never look one up by
        name. Questions sharing a label would inherit each other's options, and
        option text is related(store=True) to the master, so editing one master
        option rewrites every question that borrowed it."""
        self.ensure_one()
        QDim = self.env["etp.assessment.pro.question.dimension"]
        for spec in self._dimension_specs():
            options = spec["options"]
            if not options:
                continue
            label = spec["label"]
            correct = {c.strip().casefold() for c in spec["correct"]}
            QDim.create({
                "question_id": bank_question.id,
                "name": label,
                "option_line_ids": [
                    (0, 0, {
                        "name": o,
                        "sequence": (i + 1) * 10,
                        "is_correct": (o or "").strip().casefold() in correct,
                    })
                    for i, o in enumerate(options)
                ],
            })
            if not correct:
                _logger.warning(
                    "Draft %s dim %r: no correct option resolved; approved "
                    "with no answer key for this dimension.", self.id, label)

    def _materialize_images(self, bank_question):
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
            image = Image.create(vals)
            if bank_question.question_type == "image_label" and slot == "single":
                self._carry_label_capture(image)
            self._carry_or_detect_label(bank_question, image, slot, spec)

    def _carry_label_capture(self, image):
        """Leave detections_json empty here: setting it makes the detect cron
        skip the image, and the live DOM capture must run first."""
        self.ensure_one()
        src = (self.source_url or "").strip()
        if not src:
            return
        vals = {"source_url": src}
        if (self.capture_config_json or "").strip():
            vals["capture_config_json"] = self.capture_config_json
        if (self.omit_spec_json or "").strip():
            vals["omit_spec_json"] = self.omit_spec_json
        if self.coverage_expected:
            vals["coverage_expected"] = self.coverage_expected
        if (self.label_application or "").strip():
            vals["label_application"] = self.label_application
        if (self.label_boxes_json or "").strip():
            vals["label_boxes_json"] = self.label_boxes_json
        if (self.behavioural_key_json or "").strip():
            vals["behavioural_key_json"] = self.behavioural_key_json
        if (self.omitted_element_json or "").strip():
            vals["omitted_element_json"] = self.omitted_element_json
        image.write(vals)

    @staticmethod
    def _inline_image_bytes(spec):
        import base64 as _b64
        from ..services import image_ingest
        data = (spec.get("data") or "").strip()
        if not data:
            candidate = (spec.get("url") or spec.get("src") or "").strip()
            if candidate.startswith("data:"):
                data = candidate
        if not data:
            return b""
        payload, _ctype = image_ingest._strip_data_url(data)
        if not payload or not image_ingest._is_valid_b64(payload):
            return b""
        try:
            return _b64.b64decode(payload)
        except (ValueError, TypeError):
            return b""

    def _carry_or_detect_label(self, bank_question, image, slot, spec):
        self.ensure_one()
        if bank_question.question_type != "image_label" or slot != "single":
            return
        if (image.source_url or "").strip():
            return
        if (self.behavioural_key_json or "").strip():
            return
        if self._carry_render_detection(image, spec):
            return
        raw = self._inline_image_bytes(spec)
        if not raw:
            return
        image._detect_inline(raw, ui=(bank_question.detection_mode == "ui"))

    @staticmethod
    def _carry_render_detection(image, spec):
        if not isinstance(spec, dict):
            return False
        det = (spec.get("detections_json") or "").strip()
        if not det:
            return False
        from ..services import image_ingest
        vals = {"detections_json": det}
        annotated = (spec.get("annotated_data") or "").strip()
        if annotated:
            payload, _ctype = image_ingest._strip_data_url(annotated)
            if payload and image_ingest._is_valid_b64(payload):
                vals["annotated_image"] = payload
        if (spec.get("annotated_url") or "").strip():
            vals["annotated_image_url"] = spec["annotated_url"]
        image.write(vals)
        return True

    def _apply_authored_label_key(self, bank_question):
        """Sets detections_json, which makes the detect cron skip this image, so
        the authored key is never overwritten."""
        import json as _json
        import base64
        self.ensure_one()
        if not (self.behavioural_key_json or "").strip():
            return
        img = bank_question.image_ids.filtered(
            lambda i: i.slot == "single")[:1]
        if not img:
            return
        if (img.source_url or "").strip():
            return
        try:
            geometry = _json.loads(self.label_boxes_json or "[]")
        except (ValueError, TypeError):
            geometry = []
        vals = {
            "behavioural_key_json": self.behavioural_key_json or False,
            "coverage_expected": self.coverage_expected or "yes",
            "omitted_element_json": self.omitted_element_json or False,
            "label_application": self.label_application or False,
            "label_boxes_json": self.label_boxes_json or False,
        }
        dets = [
            {"box_2d": g["box_2d"], "label": g.get("label") or "",
             "description": g.get("description") or ""}
            for g in geometry
            if isinstance(g, dict)
            and isinstance(g.get("box_2d"), (list, tuple))
            and len(g["box_2d"]) == 4]
        raw = img._source_image_bytes()
        if raw and dets:
            from ..services import imaging, image_ingest
            annotated_png, label_key = imaging.annotate_image(raw, dets)
            annotated_b64 = base64.b64encode(annotated_png).decode()
            url, stored_b64 = image_ingest.ingest(
                self.env, None, "data:image/png;base64,%s" % annotated_b64,
                key_hint="labelauth-%s" % img.id)
            vals["detections_json"] = _json.dumps(
                label_key, ensure_ascii=False)
            vals["annotated_image"] = stored_b64 or annotated_b64
            if url:
                vals["annotated_image_url"] = url
        img.write(vals)

    def action_deny(self):
        drafts = self.filtered(lambda r: r.state == "draft")
        drafts.write({"state": "denied"})
        self._log_drafts_to_generators(drafts, "denied")
        return True

    def _briefs(self):
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

    def _verify_flaw_render_on(self):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "etp_assessment_pro.verify_flaw_render", "1")
        return str(val).strip().lower() not in ("0", "false", "no", "off", "")

    def _planted_flaws(self):
        self.ensure_one()
        plan = parse_flaw_plan(self.flaw_plan_json)
        planted = plan.get("planted") if isinstance(plan, dict) else None
        if not isinstance(planted, dict):
            return {}
        return {
            "a": [str(f) for f in (planted.get("a") or []) if str(f).strip()],
            "b": [str(f) for f in (planted.get("b") or []) if str(f).strip()],
        }

    def _dense_preview_dets(self):
        """Must stay in sync with question_image._dense_detections so the draft
        preview and the approved fallback draw the same boxes."""
        import json as _json
        self.ensure_one()
        raw = (self.label_boxes_json or "").strip()
        if not raw:
            return []
        try:
            geometry = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        dets = []
        for g in geometry if isinstance(geometry, list) else []:
            if (isinstance(g, dict)
                    and isinstance(g.get("box_2d"), (list, tuple))
                    and len(g["box_2d"]) == 4):
                dets.append({
                    "box_2d": list(g["box_2d"]),
                    "label": str(g.get("label") or "").strip(),
                    "description": str(g.get("description") or "").strip(),
                })
        return dets

    def _draw_dense_preview(self, images):
        import base64 as _b64
        import json as _json
        self.ensure_one()
        dets = self._dense_preview_dets()
        if not dets:
            return images
        from ..services import imaging
        for spec in images:
            if not isinstance(spec, dict):
                continue
            if (spec.get("slot") or "single") != "single":
                continue
            if (spec.get("annotated_data") or "").strip():
                continue
            raw = self._inline_image_bytes(spec)
            if not raw:
                continue
            try:
                annotated_png, label_key = imaging.annotate_image(raw, dets)
            except Exception:  # noqa: BLE001 - preview draw must never fail render
                _logger.exception(
                    "Dense preview draw failed for draft %s", self.id)
                continue
            spec["annotated_data"] = (
                "data:image/png;base64,%s"
                % _b64.b64encode(annotated_png).decode())
            spec["detections_json"] = _json.dumps(label_key, ensure_ascii=False)
        return images

    def _capture_source_url_on_render(self, images):
        import base64 as _b64
        import json as _json
        self.ensure_one()
        src = (self.source_url or "").strip()
        if not src:
            return False
        from ..services import dom_capture
        if not dom_capture.PLAYWRIGHT_AVAILABLE:
            return False
        single = next((s for s in images if isinstance(s, dict)
                       and (s.get("slot") or "single") == "single"), None)
        if single is None:
            return False
        # Keep in sync with question_image._capture_kwargs.
        kwargs = {}
        try:
            cfg = _json.loads(self.capture_config_json or "{}")
            if isinstance(cfg, dict):
                vp = cfg.get("viewport")
                if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
                    kwargs["viewport"] = (int(vp["width"]), int(vp["height"]))
                if cfg.get("wait_ms"):
                    kwargs["wait_ms"] = int(cfg["wait_ms"])
                if cfg.get("dismiss"):
                    kwargs["dismiss"] = cfg["dismiss"]
        except (ValueError, TypeError):
            pass
        try:
            omit = _json.loads(self.omit_spec_json or "null")
            if isinstance(omit, dict) and omit:
                kwargs["omit"] = omit
        except (ValueError, TypeError):
            pass
        try:
            result = dom_capture.capture_and_annotate(src, **kwargs)
        except Exception:  # noqa: BLE001 - capture must never break the render
            _logger.exception(
                "Render-time DOM capture failed for draft %s (%s); falling back "
                "to the dense guessed preview", self.id, src)
            return False
        if not (result and result.get("dom_manifest")):
            return False
        annotated_png = result.get("annotated_png")
        if not annotated_png:
            return False
        single["annotated_data"] = (
            "data:image/png;base64,%s"
            % _b64.b64encode(annotated_png).decode())
        base_png = result.get("screenshot_png")
        if base_png:
            single["data"] = (
                "data:image/png;base64,%s"
                % _b64.b64encode(base_png).decode())
        label_key = result.get("label_key")
        if label_key:
            single["detections_json"] = _json.dumps(
                label_key, ensure_ascii=False)
        vals = {"source_url": src}
        bkey = result.get("behavioural_key")
        if bkey:
            vals["behavioural_key_json"] = _json.dumps(
                bkey, ensure_ascii=False)
        manifest = result.get("dom_manifest")
        if manifest:
            vals["label_boxes_json"] = _json.dumps(
                manifest, ensure_ascii=False)
        if result.get("omitted_element"):
            vals["omitted_element_json"] = _json.dumps(
                result["omitted_element"], ensure_ascii=False)
        if result.get("coverage_expected"):
            vals["coverage_expected"] = result["coverage_expected"]
        try:
            self.write(vals)
        except Exception:  # noqa: BLE001 - never fail the render on a write
            _logger.exception(
                "Failed to persist capture key on draft %s", self.id)
        return True

    def _plant_defects_on_render(self, images):
        """DEFECT form (q7r) true-by-construction render. The rendered 'single'
        image is the CLEAN base; plant every defect with PIL at a known pixel and
        stamp the numbered marker on the ACTUAL drawn region, then write the
        worker-facing original (with defects, NO markers) as the stimulus and the
        annotated (with markers) as the answer-key overlay. The answer key is true
        by construction, so no marker ever lands on empty space."""
        import base64 as _b64
        import json as _json
        self.ensure_one()
        try:
            plan = _json.loads(self.defect_plan_json or "{}")
        except (ValueError, TypeError):
            plan = {}
        defects = plan.get("defects") if isinstance(plan, dict) else None
        if not isinstance(defects, list) or not defects:
            return images
        from ..services import defect_render, image_ingest
        for spec in images:
            if not isinstance(spec, dict):
                continue
            if (spec.get("slot") or "single") != "single":
                continue
            raw = self._inline_image_bytes(spec)
            if not raw:
                continue
            try:
                original_png, annotated_png, planted = defect_render.plant(
                    raw, defects, seed=abs(hash("draft-%s" % self.id)) % 100000)
            except Exception:  # noqa: BLE001 - injection must never fail the render
                _logger.exception(
                    "Defect injection failed for draft %s; leaving base image",
                    self.id)
                continue
            # Worker sees the ORIGINAL (defects present, no markers). The annotated
            # overlay + the planted key are the answer sheet the reviewer/scorer use.
            spec["data"] = ("data:image/png;base64,%s"
                            % _b64.b64encode(original_png).decode())
            spec["annotated_data"] = ("data:image/png;base64,%s"
                                      % _b64.b64encode(annotated_png).decode())
            # Persist the true-by-construction key as detections_json in the same
            # {number, label, description, box_px} shape the label UI/scoring read.
            key = [{"number": p["marker"],
                    "label": (p.get("flaw") or "")[:80],
                    "description": p.get("flaw") or "",
                    "box_px": [p["marker_xy"][0] - 20, p["marker_xy"][1] - 20,
                               p["marker_xy"][0] + 20, p["marker_xy"][1] + 20]}
                   for p in planted]
            spec["detections_json"] = _json.dumps(key, ensure_ascii=False)
            try:
                url, _stored = image_ingest.ingest(
                    self.env, None, spec["annotated_data"],
                    key_hint="defect-%s-single" % self.id)
                if url:
                    spec["annotated_url"] = url
            except Exception:  # noqa: BLE001 - overlay storage is best-effort
                _logger.exception("defect overlay ingest failed on draft %s", self.id)
        return images

    def _detect_label_on_render(self, images):
        self.ensure_one()
        if self.question_type != "image_label" or not isinstance(images, list):
            return images
        # DEFECT form (q7r): a defect plan means the rendered image is the CLEAN
        # base; plant each defect and stamp the marker on the ACTUAL drawn region
        # (true by construction — no misplaced labels).
        if (self.defect_plan_json or "").strip():
            return self._plant_defects_on_render(images)
        if (self.source_url or "").strip():
            if self._capture_source_url_on_render(images):
                return images
            return self._draw_dense_preview(images)
        # SYNTHETIC image (model-rendered, no live page): DETECT-AFTER-RENDER
        # (research renderers/ui.py). We do NOT draw the generator's guessed
        # `label_boxes_json` here — those coordinates were authored by the TEXT
        # model before the screenshot existed, so they never align with the
        # rendered pixels (the "labels at the wrong positions" bug). The box
        # GEOMETRY must come from vision detection on the actual render; the
        # authored behavioural key (per-box functionality) is preserved
        # independently and reconciled against the detected boxes. Previously an
        # authored behavioural_key_json short-circuited detection and shipped the
        # guessed boxes — that is exactly what produced misplaced labels.
        QImage = self.env["etp.assessment.pro.question.image"]
        ui = (self.detection_mode == "ui")
        for spec in images:
            if not isinstance(spec, dict):
                continue
            if (spec.get("slot") or "single") != "single":
                continue
            if (spec.get("detections_json") or "").strip():
                continue
            raw = self._inline_image_bytes(spec)
            if not raw:
                continue
            try:
                with self.env.cr.savepoint():
                    core = QImage._annotate_bytes_core(
                        raw, ui=ui,
                        usage_ctx={"prompt_id": self.prompt_id.id or False,
                                   "note": (self.name or "")[:80]},
                        key_hint="draftannot-%s-single" % self.id)
            except Exception:  # noqa: BLE001 - detection must never fail render
                _logger.exception(
                    "Render-time detect failed for draft %s; leaving the image "
                    "keyless for the detect cron to retry after approval",
                    self.id)
                continue
            if not core:
                continue
            spec["detections_json"] = core["detections_json"]
            spec["annotated_data"] = (
                "data:image/png;base64,%s" % core["annotated_b64"])
            if core["annotated_url"]:
                spec["annotated_url"] = core["annotated_url"]
        return images

    def _render_all_images(self):
        """Only stamp 'rendered' once every brief has an image, and always keep
        slots that already rendered: each render is paid for, so a retry must
        re-render only the missing slots."""
        import json as _json
        from ..services import vertex
        self.ensure_one()
        briefs = self._briefs()
        renderable = [b for b in briefs
                      if isinstance(b, dict) and b.get("prompt")]
        if not renderable:
            return False

        have = {img.get("slot"): img for img in self._current_images()
                if isinstance(img, dict) and (img.get("data") or img.get("url"))}
        todo = [b for b in renderable
                if (b.get("slot") or "single") not in have]
        if not todo:
            fresh = list(have.values())
            fresh = self._detect_label_on_render(fresh)
            self.write({
                "images_json": _json.dumps(fresh, ensure_ascii=False),
                "image_state": "rendered",
                "image_render_attempts": 0,
            })
            return True

        try:
            new_images = vertex.render_draft_images(
                self.env, todo,
                usage_ctx={"operation": "generate_image",
                           "prompt_id": self.prompt_id.id,
                           "note": self.name})
        except vertex.VertexQuotaError as exc:
            # Persist slots that rendered before the 429 so they are not re-paid.
            salvaged = {**have}
            for img in (getattr(exc, "partial", None) or []):
                if isinstance(img, dict) and img.get("slot"):
                    salvaged[img["slot"]] = img
            if len(salvaged) > len(have):
                self.write({
                    "images_json": _json.dumps(
                        list(salvaged.values()), ensure_ascii=False),
                    "image_state": "pending",
                })
            else:
                self.write({"image_state": "pending"})
            return False

        merged = {**have}
        for img in (new_images or []):
            if isinstance(img, dict) and img.get("slot"):
                merged[img["slot"]] = img
        images = list(merged.values())

        if len(images) >= len(renderable):
            verification = None
            if (self.question_type == "image_ab" and self.flaw_plan_json
                    and self._verify_flaw_render_on()):
                planted = self._planted_flaws()
                if planted.get("a") or planted.get("b"):
                    try:
                        images, verification = \
                            vertex.verify_and_regenerate_ab_images(
                                self.env, briefs, images, planted,
                                usage_ctx={"operation": "verify_planted_flaws",
                                           "prompt_id": self.prompt_id.id,
                                           "note": self.name})
                    except vertex.VertexQuotaError:
                        self.write({
                            "images_json": _json.dumps(
                                images, ensure_ascii=False),
                            "image_state": "pending",
                        })
                        return False
                    except Exception:  # noqa: BLE001 - never break generation
                        _logger.exception(
                            "etp_assessment flaw verification failed for draft "
                            "%s; storing rendered images unverified", self.id)
                        verification = None
            images = self._detect_label_on_render(images)
            vals = {
                "images_json": _json.dumps(images, ensure_ascii=False),
                "image_state": "rendered",
                "image_render_attempts": 0,
            }
            if verification is not None:
                vals["verification_json"] = _json.dumps(
                    verification, ensure_ascii=False)
            self.write(vals)
            return True

        # Incomplete render (a slot came back empty, not a 429): persist what we
        # have so it is not re-paid.
        attempts = (self.image_render_attempts or 0) + 1
        vals = {
            "image_render_attempts": attempts,
            "image_state": "failed" if attempts >= 3 else "pending",
        }
        if len(images) > len(have):
            vals["images_json"] = _json.dumps(images, ensure_ascii=False)
        self.write(vals)
        return False

    @api.model
    def _cron_render_pending_images(self):
        """The advisory lock must stay SESSION-level: an xact lock would release
        at the first per-draft commit. Commit per draft so an Odoo cron-timeout
        kill cannot roll back — and re-pay for — images already rendered."""
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_IMAGE_RENDER,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            drafts = self.search([
                ("question_type", "in", list(IMAGE_QUESTION_TYPES)),
                ("image_state", "=", "pending"),
                ("image_brief_json", "!=", False),
            ], limit=2)
            if not drafts:
                return
            _logger.info(
                "etp_assessment image cron: %d pending draft(s) to render",
                len(drafts))
            rendered = 0
            for draft in drafts:
                try:
                    with self.env.cr.savepoint():
                        if draft._render_all_images():
                            rendered += 1
                    self.env.cr.commit()
                except Exception:  # noqa: BLE001 - isolate per draft
                    self.env.cr.rollback()
                    _logger.exception(
                        "Auto-render failed for draft %s", draft.id)
                    draft.write({"image_state": "failed"})
                    self.env.cr.commit()
                    continue
            _logger.info(
                "etp_assessment image cron: rendered %d/%d draft(s)",
                rendered, len(drafts))
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_IMAGE_RENDER,))

    def _video_briefs(self):
        import json as _json
        self.ensure_one()
        raw = (self.video_brief_json or "").strip()
        if not raw or raw in ("[]", "{}"):
            return []
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return []
        return [b for b in parsed
                if isinstance(b, dict) and (b.get("prompt") or "").strip()]

    def _video_ops(self):
        import json as _json
        self.ensure_one()
        try:
            ops = _json.loads(self.video_op_json or "{}")
        except (ValueError, TypeError):
            return {}
        return ops if isinstance(ops, dict) else {}

    def _video_files(self):
        import json as _json
        self.ensure_one()
        raw = (self.video_files_json or "").strip()
        if not raw or raw in ("[]", "{}"):
            return []
        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        return [f for f in parsed if isinstance(f, dict)] \
            if isinstance(parsed, list) else []

    def _submit_video_ops(self):
        """A slot that already carries an op_name must never be re-submitted:
        the stored op_name is the idempotency handle against double-billing."""
        import json as _json
        from ..services import vertex
        self.ensure_one()
        briefs = self._video_briefs()
        if not briefs:
            return False
        ops = self._video_ops()
        for brief in briefs:
            slot = brief.get("slot") or "single"
            existing = ops.get(slot)
            if isinstance(existing, dict) and existing.get("op_name"):
                continue
            try:
                op_name = vertex.submit_video_op(
                    self.env, brief, prompt_id=self.prompt_id.id or False)
            except vertex.VertexQuotaError:
                break
            ops[slot] = {"op_name": op_name, "state": "submitted",
                         "attempts": 0,
                         "label": brief.get("label") or slot.title()}
        slots = {b.get("slot") or "single" for b in briefs}
        all_submitted = all(
            isinstance(ops.get(s), dict) and ops[s].get("op_name")
            for s in slots)
        vals = {"video_op_json": _json.dumps(ops, ensure_ascii=False)}
        if all_submitted:
            vals["video_state"] = "generating"
            vals["video_error"] = False
        else:
            vals["video_state"] = "pending"
        self.write(vals)
        return all_submitted

    def _poll_video_ops(self):
        import json as _json
        from ..services import vertex, image_ingest
        self.ensure_one()
        ops = self._video_ops()
        if not ops:
            return False
        model = vertex._video_model(self.env)
        location = vertex._video_location(self.env)
        files = {f.get("slot"): f for f in self._video_files()
                 if isinstance(f, dict) and f.get("slot")}
        changed = False
        failed_error = None
        for slot, op in list(ops.items()):
            if not isinstance(op, dict) or op.get("state") == "done":
                continue
            op_name = op.get("op_name")
            if not op_name:
                continue
            try:
                result = vertex.fetch_video_op(
                    self.env, op_name, model=model, location=location)
            except vertex.VertexQuotaError:
                continue
            except Exception as exc:  # noqa: BLE001 - bounded, per-op
                op["attempts"] = (op.get("attempts") or 0) + 1
                changed = True
                if op["attempts"] >= _VIDEO_OP_MAX_ATTEMPTS:
                    op["state"] = "failed"
                    failed_error = str(exc)[:200]
                continue
            if not result.get("done"):
                continue
            b64 = result.get("video_b64")
            gcs = result.get("gcs_uri")
            if result.get("error") or (not b64 and not gcs):
                op["attempts"] = (op.get("attempts") or 0) + 1
                changed = True
                if op["attempts"] >= _VIDEO_OP_MAX_ATTEMPTS:
                    op["state"] = "failed"
                    failed_error = result.get("error") \
                        or "op done but returned no video bytes/GCS uri"
                continue
            url, stored_b64 = (False, False)
            if b64:
                url, stored_b64 = image_ingest.ingest(
                    self.env, None, b64, content_type="video/mp4",
                    key_hint="qvideo-gen-%s-%s" % (self.id, slot))
            elif gcs:
                url = gcs
            files[slot] = {
                "slot": slot,
                "label": op.get("label") or slot.title(),
                "url": url or False,
                "data": ("data:video/mp4;base64,%s" % stored_b64)
                        if stored_b64 else False,
            }
            op["state"] = "done"
            changed = True
        vals = {}
        if changed:
            vals["video_op_json"] = _json.dumps(ops, ensure_ascii=False)
            vals["video_files_json"] = _json.dumps(
                list(files.values()), ensure_ascii=False)
        any_failed = any(isinstance(o, dict) and o.get("state") == "failed"
                         for o in ops.values())
        all_done = bool(ops) and all(
            isinstance(o, dict) and o.get("state") == "done"
            for o in ops.values())
        if any_failed:
            vals["video_state"] = "failed"
            vals["video_error"] = failed_error or "video generation failed"
        elif all_done:
            vals["video_state"] = "rendered"
            vals["video_error"] = False
        if vals:
            self.write(vals)
        return vals.get("video_state") == "rendered"

    def _materialize_videos(self, bank_question):
        self.ensure_one()
        files = self._video_files()
        if not files:
            return
        from ..services import image_ingest
        Video = self.env["etp.assessment.pro.question.video"]
        seq = {"reference": 10, "output": 20, "single": 30}
        for f in files:
            slot = (f.get("slot") or "single").strip().lower()
            if slot not in ("reference", "output", "single"):
                slot = "single"
            url = f.get("url") or False
            b64 = False
            data = f.get("data")
            if not url and data:
                url, b64 = image_ingest.ingest(
                    self.env, None, data, content_type="video/mp4",
                    key_hint="qvideo-%s-%s" % (bank_question.id, slot))
            Video.create({
                "question_id": bank_question.id,
                "slot": slot,
                "label": f.get("label") or slot.title(),
                "video_url": url or False,
                "video": b64 or False,
                "sequence": seq.get(slot, 30),
            })

    def _submit_pending_video_ops(self):
        from ..services import vertex
        if not vertex.video_generation_available(self.env):
            return
        drafts = self.search([
            ("question_type", "in", list(VIDEO_QUESTION_TYPES)),
            ("video_state", "=", "pending"),
            ("video_brief_json", "!=", False),
        ], limit=2)
        for draft in drafts:
            try:
                with self.env.cr.savepoint():
                    draft._submit_video_ops()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001 - isolate per draft
                self.env.cr.rollback()
                _logger.exception(
                    "Video op submit failed for draft %s", draft.id)
                draft.write({"video_state": "failed",
                             "video_error": "submit failed"})
                self.env.cr.commit()

    def _poll_generating_video_ops(self):
        drafts = self.search([
            ("question_type", "in", list(VIDEO_QUESTION_TYPES)),
            ("video_state", "=", "generating"),
        ], limit=4)
        for draft in drafts:
            try:
                with self.env.cr.savepoint():
                    draft._poll_video_ops()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001 - isolate per draft
                self.env.cr.rollback()
                _logger.exception(
                    "Video op poll failed for draft %s", draft.id)
                draft.write({"video_state": "failed",
                             "video_error": "poll failed"})
                self.env.cr.commit()

    @api.model
    def _cron_poll_video_ops(self):
        """The advisory lock must stay SESSION-level: an xact lock would release
        at the first per-draft commit."""
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_VIDEO_POLL,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            self._submit_pending_video_ops()
            self._poll_generating_video_ops()
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_VIDEO_POLL,))

    def action_apply_uploaded_image(self):
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


# Formats _extract_text() intentionally does NOT extract: they are passed to the
# multimodal model as inline document parts instead. Kept beside _extract_text so
# the two cannot drift apart - _compute_status reads this to avoid flagging them.
NATIVE_DOC_EXTENSIONS = ("pdf", "png", "jpg", "jpeg", "webp", "gif")


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
        [("sop", "SOP"), ("reference", "Reference"), ("sample", "Sample Questions"),
         ("vendor", "Vendor"), ("client", "Client"), ("other", "Other")],
        default="other", required=True, index=True,
    )
    file = fields.Binary(string="File", attachment=True, required=True)
    extracted_text = fields.Text(readonly=True)
    extraction_error = fields.Char(readonly=True)
    char_count = fields.Integer(
        compute="_compute_char_count", store=True
    )
    status = fields.Selection(
        [("pending", "Uploaded"), ("ready", "Ready"), ("native", "Sent natively"),
         ("failed", "Failed")],
        compute="_compute_status", string="Status",
        help="Ready = text extracted; Sent natively = PDF/image read directly by the "
             "multimodal model, so no text extraction is expected; Uploaded = stored, "
             "text not extracted yet; Failed = extraction error.")

    @api.depends("extracted_text")
    def _compute_char_count(self):
        for rec in self:
            rec.char_count = len(rec.extracted_text or "")

    @api.depends("extracted_text", "extraction_error", "file", "name")
    def _compute_status(self):
        for rec in self:
            ext = (rec.name or "").rsplit(".", 1)[-1].lower()
            if rec.extraction_error:
                rec.status = "failed"
            elif (rec.extracted_text or "").strip():
                rec.status = "ready"
            elif ext in NATIVE_DOC_EXTENSIONS:
                # _extract_text() deliberately returns empty-with-no-error for these:
                # they go to the model as inline document parts. Reporting them as
                # "pending" left a permanent amber dot on a perfectly good PDF.
                rec.status = "native"
            else:
                rec.status = "pending"

    @staticmethod
    def _extract_docx(raw):
        import io
        import re as _re
        import zipfile
        try:
            from defusedxml.ElementTree import fromstring as _xml_fromstring
        except ImportError as exc:
            # H-12: do NOT silently fall back to stdlib xml.etree — it is exposed
            # to billion-laughs entity expansion on an attacker .docx. defusedxml
            # is a declared manifest dependency; if it is missing, fail loudly.
            raise RuntimeError(
                "defusedxml is required to parse .docx uploads safely. "
                "Install it: pip install defusedxml") from exc

        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # H-13: cap the UNCOMPRESSED size before reading. A ~10MB deflate
            # zip can hold a >10GB member (zip bomb) that OOMs the worker when
            # zf.read decompresses it whole. 40MB is far above any real SOP.
            _MAX_DOCX_XML = 40 * 1024 * 1024
            try:
                info = zf.getinfo("word/document.xml")
            except KeyError:
                raise ValueError("docx has no word/document.xml")
            if info.file_size > _MAX_DOCX_XML:
                raise ValueError(
                    "docx word/document.xml is %d bytes (> %d cap); refused as "
                    "a possible zip bomb." % (info.file_size, _MAX_DOCX_XML))
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
            if ext in NATIVE_DOC_EXTENSIONS:
                # Sent natively to the model by _sop_doc_parts: empty
                # extracted_text is expected here, not an error.
                return "", False
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
