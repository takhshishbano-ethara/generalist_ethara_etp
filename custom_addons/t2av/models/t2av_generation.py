"""T2AV video-generation job — the parent of one-to-three attempts.

A *t2av.generation* row represents one creative goal owned by one
user. The user iterates toward that goal through up to **three
attempts**: the original generation (#1) plus up to two **refinements**
where they revised one or more input fields to fix something they
didn't like in the previous result. Each attempt is a full OpenRouter
submission with its own state, costs, and S3 object — the job
aggregates them.

This module owns the *job* model only. The pipeline (submit / poll /
download) lives on ``t2av.attempt`` (see ``t2av_attempt.py``);
landing in Wave 4D. Everything on the job here is either a user input
that gets *copied* onto the next attempt at spawn time, or a *computed
proxy* mirroring the active attempt for list-view sorting/filtering
and form-view convenience.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import credential_manager

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine constants — kept at module level for migration / test imports.
# The state machine itself lives on ``t2av.attempt``; the job's ``state``
# is a stored compute that mirrors the active attempt.
# ---------------------------------------------------------------------------
_STATE_NOT_ASSIGNED = "not_assigned"
_STATE_DRAFT = "draft"
_STATE_QUEUED = "queued"
_STATE_SUBMITTING = "submitting"
_STATE_PROCESSING = "processing"
_STATE_DOWNLOADING = "downloading"
_STATE_DONE = "done"
_STATE_FAILED = "failed"
_STATE_CANCELLED = "cancelled"

_NON_TERMINAL = {_STATE_QUEUED, _STATE_SUBMITTING, _STATE_PROCESSING, _STATE_DOWNLOADING}
_TERMINAL = {_STATE_DONE, _STATE_FAILED, _STATE_CANCELLED}

MAX_ATTEMPTS = 3

CATEGORY_SELECTION = [
    ("animals_wildlife",       "Animals Wildlife"),
    ("animated_styles",        "Animated Styles"),
    ("animated_text",          "Animated Text"),
    ("av_sync_sound_effects",  "AV Sync Sound Effects"),
    ("camera_motion",          "Camera Motion"),
    ("educational_videos",     "Educational Videos"),
    ("fantasy_surreal",        "Fantasy Surreal"),
    ("fine_grained_motion",    "Fine Grained Motion"),
    ("high_motion_action",     "High Motion Action"),
    ("human_activities",       "Human Activities"),
    ("multi_speaker_dialogue", "Multi-Speaker Dialogue"),
    ("music_performance",      "Music Performance"),
    ("narrative_cinematic",    "Narrative Cinematic"),
    ("natural_patterns",       "Natural Patterns"),
    ("nature_weather",         "Nature Weather"),
    ("person_emoting",         "Person Emoting"),
    ("speech_styles",          "Speech Styles"),
    ("urban_scenes",           "Urban Scenes"),
    ("vehicles_machines",      "Vehicles Machines"),
]


class T2AVGeneration(models.Model):
    _name = "t2av.generation"
    _description = "T2AV Video Generation Job"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "name"

    # ------------------------------------------------------------------
    # Identity / ownership
    # ------------------------------------------------------------------
    name = fields.Char(
        string="Reference",
        default=lambda self: self._default_name(),
        readonly=True,
        copy=False,
        tracking=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        default=lambda self: self.env.user,
        required=True,
        index=True,
        ondelete="restrict",
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # User inputs — values for the NEXT attempt. Each attempt snapshots
    # these at spawn time so editing them later doesn't rewrite history.
    # ------------------------------------------------------------------
    prompt = fields.Text(
        string="Prompt",
        required=True,
        tracking=True,
    )
    negative_prompt = fields.Text(string="Negative Prompt")
    duration = fields.Selection(
        [
            ("4", "4s"), ("5", "5s"), ("6", "6s"), ("7", "7s"), ("8", "8s"),
            ("9", "9s"), ("10", "10s"), ("12", "12s"), ("15", "15s"),
        ],
        string="Duration",
        default=lambda self: self._default_duration(),
        required=True,
    )
    resolution = fields.Selection(
        [("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        string="Resolution",
        default=lambda self: self._default_resolution(),
        required=True,
    )
    aspect_ratio = fields.Selection(
        [
            ("16:9", "16:9"), ("9:16", "9:16"), ("1:1", "1:1"),
            ("4:3", "4:3"), ("3:4", "3:4"), ("21:9", "21:9"),
        ],
        string="Aspect Ratio",
        default="16:9",
        required=True,
    )
    seed = fields.Integer(string="Seed", default=0)
    generate_audio = fields.Boolean(string="Generate Audio", default=True)
    model_name = fields.Char(
        string="Model",
        default=lambda self: self._default_model_name(),
        readonly=True,
    )

    sub_category = fields.Char(string="Sub-Category", tracking=True)
    style = fields.Selection(
        [
            ("casual", "Casual"),
            ("precise", "Precise"),
            ("narrative", "Narrative"),
            ("terse", "Terse"),
            ("exhaustive", "Exhaustive"),
            ("creative", "Creative"),
        ],
        string="Style", tracking=True,
        help="Drives enrichment voice and word band per enhance.md.",
    )
    priority = fields.Selection(
        [("medium", "Medium"), ("high", "High"), ("highest", "Highest")],
        string="Priority", default="medium", tracking=True,
    )
    topic = fields.Char(string="Topic", tracking=True)
    language = fields.Char(string="Language", default="English", tracking=True)
    speaker_count = fields.Integer(string="Speaker Count", default=0, tracking=True)
    dialogue_transcript = fields.Text(string="Dialogue Transcript")
    complexity = fields.Selection(
        [("simple", "Simple"), ("moderate", "Moderate"), ("complex", "Complex")],
        string="Complexity", default="moderate", tracking=True,
    )
    enriched_prompt = fields.Text(
        string="Enriched Prompt", readonly=True, copy=False,
        help="LLM-enriched text. Populated by Enrich on CLEAN or WARNED.",
    )
    golden_prompt = fields.Text(
        string="Golden Prompt", readonly=True, copy=False,
        help="The enriched prompt that PASSED the T2AV validator "
             "with zero warnings and zero fatals. Ready for video generation.",
    )
    is_golden = fields.Boolean(
        string="Is Golden", compute="_compute_is_golden", store=True,
    )

    # ------------------------------------------------------------------
    # Dataset category (v1.2) — required at submit time, locks after the
    # first successful attempt.
    # ------------------------------------------------------------------
    category = fields.Selection(
        CATEGORY_SELECTION,
        string="Category",
        tracking=True,
        help="Dataset category. Required before Generate. Locked once the first "
             "attempt successfully lands in S3. Drives the canonical filename "
             "T2AV_<category>_<NNNNNN>.mp4.",
    )
    category_locked = fields.Boolean(
        string="Category Locked",
        compute="_compute_category_locked",
        store=True,
        help="True once any attempt has state=done — category becomes readonly.",
    )

    # ------------------------------------------------------------------
    # Attempt linkage — the new core
    # ------------------------------------------------------------------
    attempt_ids = fields.One2many(
        "t2av.attempt",
        inverse_name="job_id",
        string="Attempts",
    )
    enrichment_ids = fields.One2many(
        "t2av.enrichment",
        inverse_name="job_id",
        string="Enrichments",
    )
    review_ids = fields.One2many(
        "t2av.video.review",
        inverse_name="job_id",
        string="Video Reviews",
    )
    review_status = fields.Selection(
        [("pass", "Pass"), ("review", "Review"), ("fail", "Fail")],
        string="Review Status",
        compute="_compute_review_status",
        store=True,
    )
    enrichments_used = fields.Integer(
        string="Enrichments Used",
        compute="_compute_enrichments_used",
        store=True,
    )
    last_enrichment_state = fields.Char(
        string="Last Enrichment State",
        compute="_compute_last_enrichment_state",
        store=True,
    )
    active_attempt_id = fields.Many2one(
        "t2av.attempt",
        string="Active Attempt",
        compute="_compute_active_attempt",
        store=True,
        help="Latest done attempt, falling back to the latest attempt of any "
             "state. Proxies for the job's lifecycle / cost / output fields.",
    )
    attempts_used = fields.Integer(
        string="Attempts Used",
        compute="_compute_attempts_counts",
        store=True,
    )
    attempts_remaining = fields.Integer(
        string="Attempts Remaining",
        compute="_compute_attempts_counts",
        store=True,
    )
    attempts_label = fields.Char(
        string="Attempts",
        compute="_compute_attempts_label",
    )

    # ------------------------------------------------------------------
    # Retry-flow flag (stored so it survives the form reload between
    # action_start_retry and action_submit_retry / action_discard_retry).
    # ------------------------------------------------------------------
    ui_retry_pending = fields.Boolean(
        string="Retry Pending",
        default=False,
        copy=False,
        help="True while the user is editing input fields before submitting "
             "the next attempt. Reset by submit / discard.",
    )

    # ------------------------------------------------------------------
    # State proxy — computed from active attempt, stored for list views
    # ------------------------------------------------------------------
    state = fields.Selection(
        [
            ("not_assigned", "Not Assigned"),
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("submitting", "Submitting"),
            ("processing", "Processing"),
            ("downloading", "Downloading"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        compute="_compute_state",
        store=True,
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
        default=_STATE_DRAFT,
    )

    source = fields.Selection(
        [("manual", "Manual"), ("import", "Import")],
        string="Source",
        default="manual",
        required=True,
        copy=False,
        index=True,
        readonly=True,
        help="How this generation was created. CSV-imported rows sit in "
             "Not Assigned until the user opens and runs them.",
    )

    # ------------------------------------------------------------------
    # Proxy fields (computed from active_attempt; stored for sort/filter)
    # ------------------------------------------------------------------
    openrouter_job_id = fields.Char(
        string="OpenRouter Job ID",
        compute="_compute_proxy_fields",
        store=True,
    )
    openrouter_status = fields.Char(
        string="API Status",
        compute="_compute_proxy_fields",
        store=True,
    )
    poll_attempts = fields.Integer(
        string="Poll Attempts",
        compute="_compute_proxy_fields",
        store=True,
    )
    last_polled_at = fields.Datetime(
        string="Last Polled At",
        compute="_compute_proxy_fields",
        store=True,
    )
    error_message = fields.Text(
        string="Error Message",
        compute="_compute_proxy_fields",
        store=True,
    )
    error_code = fields.Char(
        string="Error Code",
        compute="_compute_proxy_fields",
        store=True,
    )
    submitted_at = fields.Datetime(
        string="Submitted At",
        compute="_compute_proxy_fields",
        store=True,
    )
    completed_at = fields.Datetime(
        string="Completed At",
        compute="_compute_proxy_fields",
        store=True,
    )
    tokens_used = fields.Integer(
        string="Tokens Used",
        compute="_compute_proxy_fields",
        store=True,
    )
    video_s3_url = fields.Char(
        string="S3 URL",
        compute="_compute_proxy_fields",
        store=True,
        help="Backward-compat URL from the latest done attempt. Prefer the "
             "attempt's ``video_play_url`` for live presigned URLs.",
    )
    duration_seconds = fields.Float(
        string="Wall Time (s)",
        compute="_compute_proxy_fields",
        store=True,
    )

    # ------------------------------------------------------------------
    # Cost roll-up
    # ------------------------------------------------------------------
    cost_usd = fields.Float(
        string="Total Cost (USD)",
        digits=(12, 6),
        compute="_compute_total_cost",
        store=True,
        help="Sum of cost across all attempts.",
    )
    total_cost_usd = fields.Float(
        string="Total Cost",
        digits=(12, 6),
        compute="_compute_total_cost",
        store=True,
        help="Alias of cost_usd — exposed under both names for clarity.",
    )
    cost_usd_estimate = fields.Float(
        string="Estimated Cost (Next Attempt)",
        digits=(12, 6),
        compute="_compute_cost_estimate",
        store=True,
        help="Estimated cost of the *next* attempt based on the current job "
             "input fields. Does NOT include past attempts.",
    )
    cost_usd_delta = fields.Float(
        string="Cost Delta",
        digits=(12, 6),
        compute="_compute_cost_delta",
        store=True,
    )

    # ------------------------------------------------------------------
    # Retry count — number of attempts beyond the first
    # ------------------------------------------------------------------
    retry_count = fields.Integer(
        string="Retry Count",
        compute="_compute_retry_count",
        store=True,
    )

    # Job-level SQL constraints — most v1 constraints (openrouter_job_id
    # uniqueness, poll_attempts non-negative) moved onto the attempt.
    _sql_constraints = []

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------
    def _default_name(self):
        return self.env["ir.sequence"].next_by_code("t2av.generation") or _("New")

    def _default_resolution(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "t2av.default_resolution", "720p"
        )

    def _default_duration(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "t2av.default_duration", "5"
        )

    def _default_model_name(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "t2av.default_model", "bytedance/seedance-2.0"
        )

    # ------------------------------------------------------------------
    # Compute methods
    # ------------------------------------------------------------------
    @api.depends("attempt_ids", "attempt_ids.state", "attempt_ids.attempt_number")
    def _compute_active_attempt(self):
        """Active attempt = latest done, else latest by number, else False."""
        for rec in self:
            done = rec.attempt_ids.filtered(lambda a: a.state == _STATE_DONE).sorted(
                "attempt_number", reverse=True,
            )
            if done:
                rec.active_attempt_id = done[:1]
                continue
            latest = rec.attempt_ids.sorted("attempt_number", reverse=True)[:1]
            rec.active_attempt_id = latest if latest else False

    @api.depends("attempt_ids", "attempt_ids.attempt_number")
    def _compute_attempts_counts(self):
        for rec in self:
            used = len(rec.attempt_ids)
            rec.attempts_used = used
            rec.attempts_remaining = max(0, MAX_ATTEMPTS - used)

    @api.depends("attempts_used", "attempts_remaining")
    def _compute_attempts_label(self):
        for rec in self:
            if rec.attempts_used == 0:
                rec.attempts_label = _("No attempts yet")
            elif rec.attempts_remaining == 0:
                rec.attempts_label = _("Attempt %d/%d — max reached") % (
                    rec.attempts_used, MAX_ATTEMPTS,
                )
            else:
                rec.attempts_label = _("Attempt %d/%d") % (
                    rec.attempts_used, MAX_ATTEMPTS,
                )

    @api.depends("enrichment_ids")
    def _compute_enrichments_used(self):
        for rec in self:
            rec.enrichments_used = len(rec.enrichment_ids)

    @api.depends("golden_prompt")
    def _compute_is_golden(self):
        for rec in self:
            rec.is_golden = bool((rec.golden_prompt or "").strip())

    @api.depends(
        "attempt_ids.review_ids.effective_verdict",
        "attempt_ids.review_ids.state",
        "attempt_ids.review_ids.completed_at",
    )
    def _compute_review_status(self):
        for rec in self:
            done = rec.attempt_ids.mapped("review_ids").filtered(
                lambda r: r.state == "done"
            )
            if not done:
                rec.review_status = False
                continue
            latest = done.sorted("completed_at", reverse=True)[:1]
            effective = latest.effective_verdict
            if effective == "accept":
                rec.review_status = "pass"
            elif effective == "review":
                rec.review_status = "review"
            else:
                rec.review_status = "fail"

    @api.depends("enrichment_ids", "enrichment_ids.state", "enrichment_ids.attempt_number")
    def _compute_last_enrichment_state(self):
        for rec in self:
            last = rec.enrichment_ids.sorted("attempt_number", reverse=True)[:1]
            rec.last_enrichment_state = last.state if last else ""

    @api.depends("attempt_ids.state")
    def _compute_category_locked(self):
        for rec in self:
            rec.category_locked = any(a.state == "done" for a in rec.attempt_ids)

    @api.depends("active_attempt_id", "active_attempt_id.state", "source")
    def _compute_state(self):
        for rec in self:
            if rec.active_attempt_id:
                rec.state = rec.active_attempt_id.state
            elif rec.source == "import":
                rec.state = _STATE_NOT_ASSIGNED
            else:
                rec.state = _STATE_DRAFT

    @api.depends(
        "active_attempt_id",
        "active_attempt_id.openrouter_job_id",
        "active_attempt_id.openrouter_status",
        "active_attempt_id.poll_attempts",
        "active_attempt_id.last_polled_at",
        "active_attempt_id.error_message",
        "active_attempt_id.error_code",
        "active_attempt_id.submitted_at",
        "active_attempt_id.completed_at",
        "active_attempt_id.tokens_used",
        "active_attempt_id.video_s3_url",
        "active_attempt_id.duration_seconds",
    )
    def _compute_proxy_fields(self):
        for rec in self:
            a = rec.active_attempt_id
            rec.openrouter_job_id = a.openrouter_job_id if a else False
            rec.openrouter_status = a.openrouter_status if a else False
            rec.poll_attempts = a.poll_attempts if a else 0
            rec.last_polled_at = a.last_polled_at if a else False
            rec.error_message = a.error_message if a else False
            rec.error_code = a.error_code if a else False
            rec.submitted_at = a.submitted_at if a else False
            rec.completed_at = a.completed_at if a else False
            rec.tokens_used = a.tokens_used if a else 0
            rec.video_s3_url = a.video_s3_url if a else False
            rec.duration_seconds = a.duration_seconds if a else 0.0

    @api.depends("attempt_ids.cost_usd")
    def _compute_total_cost(self):
        for rec in self:
            total = sum(rec.attempt_ids.mapped("cost_usd"))
            rec.cost_usd = total
            rec.total_cost_usd = total

    @api.depends("resolution", "duration", "aspect_ratio")
    def _compute_cost_estimate(self):
        from ..services import cost
        for rec in self:
            try:
                _tokens, usd = cost.estimate(
                    rec.resolution,
                    int(rec.duration),
                    rec.aspect_ratio,
                )
                rec.cost_usd_estimate = usd
            except (ValueError, TypeError):
                rec.cost_usd_estimate = 0.0

    @api.depends("cost_usd", "cost_usd_estimate")
    def _compute_cost_delta(self):
        for rec in self:
            rec.cost_usd_delta = (rec.cost_usd or 0.0) - (rec.cost_usd_estimate or 0.0)

    @api.depends("attempts_used")
    def _compute_retry_count(self):
        for rec in self:
            rec.retry_count = max(0, rec.attempts_used - 1)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("prompt")
    def _check_prompt(self):
        for rec in self:
            stripped = (rec.prompt or "").strip()
            if not stripped:
                raise ValidationError(_("Prompt is required."))
            if len(stripped) > 2000:
                raise ValidationError(_("Prompt must be at most 2000 characters."))

    @api.constrains("seed")
    def _check_seed(self):
        for rec in self:
            if rec.seed < 0 or rec.seed > 2_147_483_647:
                raise ValidationError(_(
                    "Seed must be between 0 and 2,147,483,647."
                ))

    # ------------------------------------------------------------------
    # CRUD overrides — only the unlink guard remains. State is now a
    # computed proxy, so the v1 write() guard moved onto the attempt.
    # ------------------------------------------------------------------
    def unlink(self):
        for rec in self:
            if rec.state in _NON_TERMINAL:
                raise UserError(_(
                    "Cannot delete %s while an attempt is in progress."
                ) % rec.display_name)
        return super().unlink()

    # ------------------------------------------------------------------
    # Attempt spawn helper
    # ------------------------------------------------------------------
    def _spawn_attempt(self):
        """Create the next attempt row using the job's current input field values.

        Returns the new attempt. Caller is responsible for kicking off the
        OpenRouter submission via ``attempt._defer("_run_submit")``.
        """
        self.ensure_one()
        if self.attempts_used >= MAX_ATTEMPTS:
            raise UserError(_(
                "Max %(max)d attempts reached for %(name)s. "
                "Create a new generation."
            ) % {"max": MAX_ATTEMPTS, "name": self.display_name})
        next_n = (max(self.attempt_ids.mapped("attempt_number") or [0])) + 1
        attempt_prompt = (
            (self.golden_prompt or "").strip()
            or (self.enriched_prompt or "").strip()
            or self.prompt
        )
        attempt = self.env["t2av.attempt"].create({
            "job_id": self.id,
            "attempt_number": next_n,
            "prompt": attempt_prompt,
            "negative_prompt": self.negative_prompt or False,
            "duration": self.duration,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "seed": self.seed or 0,
            "generate_audio": self.generate_audio,
            "model_name": self.model_name or "bytedance/seedance-2.0",
            "state": _STATE_QUEUED,
        })
        return attempt

    # ------------------------------------------------------------------
    # Action methods — the centerpiece UX
    # ------------------------------------------------------------------
    def action_generate(self):
        """Spawn attempt #1. Guards: no existing attempts on this job."""
        self.ensure_one()
        if self.attempt_ids:
            raise UserError(_(
                "Use Retry to start another attempt — this job already has "
                "attempts."
            ))
        self._validate_can_submit()
        attempt = self._spawn_attempt()
        self.message_post(body=_("Generation queued (attempt 1)."))
        attempt._defer("_run_submit")
        return self._display_queued_notification()

    _METADATA_FIELDS_THAT_INVALIDATE_GOLDEN = (
        "prompt", "category", "sub_category", "style", "priority", "topic", "complexity",
    )

    def write(self, vals):
        if any(f in vals for f in self._METADATA_FIELDS_THAT_INVALIDATE_GOLDEN):
            if "golden_prompt" not in vals and "enriched_prompt" not in vals:
                affected = self.filtered(
                    lambda r: r.golden_prompt or r.enriched_prompt
                )
                if affected:
                    vals = dict(vals)
                    vals["golden_prompt"] = False
                    vals["enriched_prompt"] = False
                    for rec in affected:
                        rec.message_post(body=_(
                            "Golden Prompt and Enriched Prompt invalidated because "
                            "source metadata changed. Re-run Enrich to refresh."
                        ))
        return super().write(vals)

    def action_batch_enrich(self):
        _logger.info(
            "T2AV: batch-enrich invoked on %d record(s) by user %s",
            len(self), self.env.user.login,
        )
        BATCH_CAP = 100
        if len(self) > BATCH_CAP:
            raise UserError(_(
                "Batch too large: %(n)d row(s) selected, max %(cap)d per click."
            ) % {"n": len(self), "cap": BATCH_CAP})

        access_key = credential_manager.get_aws_access_key(self.env)
        secret_key = credential_manager.get_aws_secret_key(self.env)
        if not access_key or not secret_key:
            raise UserError(_(
                "AWS Access Key / Secret Key not configured. "
                "Set them in Settings > T2AV."
            ))

        eligible = self.filtered(
            lambda r: (r.prompt or "").strip()
            and not r.is_golden
            and r.last_enrichment_state not in ("queued", "submitting", "validating")
        )
        skipped_no_prompt = self.filtered(lambda r: not (r.prompt or "").strip())
        skipped_already_golden = self.filtered(lambda r: r.is_golden)
        skipped_in_flight = self.filtered(
            lambda r: r.last_enrichment_state in ("queued", "submitting", "validating")
        )

        icp = self.env["ir.config_parameter"].sudo()
        try:
            max_attempts = int(icp.get_param("t2av.enrichment_max_attempts", "3"))
        except (TypeError, ValueError):
            max_attempts = 3

        queued = 0
        errors = []
        for rec in eligible:
            try:
                if rec.enrichments_used >= max_attempts:
                    errors.append(
                        f"{rec.display_name}: max enrichment attempts ({max_attempts}) reached"
                    )
                    continue
                next_n = (max(rec.enrichment_ids.mapped("attempt_number") or [0])) + 1
                enrichment = self.env["t2av.enrichment"].create({
                    "job_id": rec.id,
                    "attempt_number": next_n,
                    "state": "queued",
                })
                rec.message_post(body=_(
                    "Batch enrichment #%d queued."
                ) % next_n)
                enrichment._defer("_run_enrich")
                queued += 1
            except Exception as exc:
                errors.append(f"{rec.display_name}: {exc}")
                _logger.exception(
                    "T2AV batch-enrich failed for job %s", rec.id,
                )

        lines = [
            _("Selected: %d row(s).") % len(self),
            _("Queued: %d") % queued,
            _("Skipped (no prompt): %d") % len(skipped_no_prompt),
            _("Skipped (already Golden): %d") % len(skipped_already_golden),
            _("Skipped (enrichment in flight): %d") % len(skipped_in_flight),
        ]
        if errors:
            lines.append(_("Errors: %d") % len(errors))
            lines.append("")
            lines.extend(errors[:10])
            if len(errors) > 10:
                lines.append(_("... and %d more error(s).") % (len(errors) - 10))

        ntype = "success" if not errors and queued > 0 else ("warning" if queued > 0 else "danger")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch Enrich"),
                "message": "\n".join(lines),
                "type": ntype,
                "sticky": True,
            },
        }

    def action_batch_run_review(self):
        _logger.info(
            "T2AV: batch-run-review invoked on %d record(s) by user %s",
            len(self), self.env.user.login,
        )
        BATCH_CAP = 50
        if len(self) > BATCH_CAP:
            raise UserError(_(
                "Batch too large: %(n)d row(s) selected, max %(cap)d per click."
            ) % {"n": len(self), "cap": BATCH_CAP})

        access_key = credential_manager.get_aws_access_key(self.env)
        secret_key = credential_manager.get_aws_secret_key(self.env)
        if not access_key or not secret_key:
            raise UserError(_(
                "AWS Access Key / Secret Key not configured."
            ))

        queued = 0
        skipped_no_video = 0
        skipped_in_flight = 0
        errors = []

        for rec in self:
            if rec.state != "done":
                skipped_no_video += 1
                continue
            done_attempts = rec.attempt_ids.filtered(
                lambda a: a.state == "done"
            ).sorted("completed_at", reverse=True)
            if not done_attempts:
                skipped_no_video += 1
                continue
            active = done_attempts[0]
            if not (active.video_play_url or active.video_s3_url):
                skipped_no_video += 1
                continue
            in_flight = rec.attempt_ids.mapped("review_ids").filtered(
                lambda r: r.state in ("queued", "submitting")
            )
            if in_flight:
                skipped_in_flight += 1
                continue
            try:
                review = self.env["t2av.video.review"].create({
                    "attempt_id": active.id,
                    "state": "queued",
                })
                rec.message_post(body=_(
                    "Batch video review queued for attempt %d."
                ) % active.attempt_number)
                review._defer("_run_review")
                queued += 1
            except Exception as exc:
                errors.append(f"{rec.display_name}: {exc}")
                _logger.exception(
                    "T2AV batch-run-review failed for job %s", rec.id,
                )

        lines = [
            _("Selected: %d row(s).") % len(self),
            _("Queued: %d") % queued,
            _("Skipped (no completed video): %d") % skipped_no_video,
            _("Skipped (review in flight): %d") % skipped_in_flight,
        ]
        if errors:
            lines.append(_("Errors: %d") % len(errors))
            lines.append("")
            lines.extend(errors[:10])

        ntype = "success" if not errors and queued > 0 else ("warning" if queued > 0 else "danger")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch Run Review"),
                "message": "\n".join(lines),
                "type": ntype,
                "sticky": True,
            },
        }

    def action_batch_generate_golden(self): 
        _logger.info(
            "T2AV: batch-generate-golden invoked on %d record(s) by user %s",
            len(self), self.env.user.login,
        )
        BATCH_CAP = 100
        if len(self) > BATCH_CAP:
            raise UserError(_(
                "Batch too large: %(n)d row(s) selected, max %(cap)d per click. "
                "Filter the list and try again."
            ) % {"n": len(self), "cap": BATCH_CAP})

        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            raise UserError(_(
                "OpenRouter API key not configured. Set it in Settings > T2AV."
            ))
        icp = self.env["ir.config_parameter"].sudo()
        try:
            connector_id = int(icp.get_param("t2av.s3_connector_id") or 0)
        except (ValueError, TypeError):
            connector_id = 0
        if not connector_id:
            raise UserError(_(
                "S3 connector not configured. Set it in Settings > T2AV."
            ))

        eligible = self.filtered(
            lambda r: (r.enriched_prompt or "").strip() and (
                not r.attempt_ids
                or all(a.state == "failed" for a in r.attempt_ids)
            )
        )
        skipped_no_enriched = self.filtered(
            lambda r: not (r.enriched_prompt or "").strip()
        )
        skipped_has_attempts = self.filtered(
            lambda r: (r.enriched_prompt or "").strip() and r.attempt_ids
            and not all(a.state == "failed" for a in r.attempt_ids)
        )

        queued = 0
        errors = []
        for rec in eligible:
            try:
                rec._validate_can_submit()
                attempt = rec._spawn_attempt()
                rec.message_post(body=_(
                    "Batch generation queued (attempt %d)."
                ) % attempt.attempt_number)
                attempt._defer("_run_submit")
                queued += 1
            except Exception as exc:
                errors.append(f"{rec.display_name}: {exc}")
                _logger.exception(
                    "T2AV batch-generate failed for job %s", rec.id,
                )

        lines = [
            _("Selected: %d row(s).") % len(self),
            _("Queued: %d") % queued,
            _("Skipped (no Enriched Prompt): %d") % len(skipped_no_enriched),
            _("Skipped (already has attempts): %d") % len(skipped_has_attempts),
        ]
        if errors:
            lines.append(_("Errors: %d") % len(errors))
            lines.append("")
            lines.extend(errors[:10])
            if len(errors) > 10:
                lines.append(_("... and %d more error(s).") % (len(errors) - 10))

        notif_type = "success"
        if errors:
            notif_type = "danger"
        elif queued == 0:
            notif_type = "warning"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Batch Generate (Bulk)"),
                "message": "\n".join(lines),
                "type": notif_type,
                "sticky": True,
            },
        }

    def action_run_review(self):
        self.ensure_one()
        if self.state != "done":
            raise UserError(_(
                "Video is not done yet. Generate the video first."
            ))
        done_attempts = self.attempt_ids.filtered(
            lambda a: a.state == "done"
        ).sorted("completed_at", reverse=True)
        if not done_attempts:
            raise UserError(_("No completed attempt to review."))
        active = done_attempts[0]
        if not (active.video_play_url or active.video_s3_url):
            raise UserError(_("Latest completed attempt has no playable video URL."))
        in_flight = self.attempt_ids.mapped("review_ids").filtered(
            lambda r: r.state in ("queued", "submitting")
        )
        if in_flight:
            raise UserError(_(
                "A video review is already in flight for this job. "
                "Wait for it to finish before queuing another."
            ))
        access_key = credential_manager.get_aws_access_key(self.env)
        secret_key = credential_manager.get_aws_secret_key(self.env)
        if not access_key or not secret_key:
            raise UserError(_(
                "AWS Access Key / Secret Key not configured. "
                "Set them in Settings > T2AV."
            ))
        review = self.env["t2av.video.review"].create({
            "attempt_id": active.id,
            "state": "queued",
        })
        self.message_post(body=_(
            "Video review queued for attempt %d."
        ) % active.attempt_number)
        review._defer("_run_review")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("T2AV"),
                "message": _(
                    "Video review queued. Refresh the form or open the "
                    "Reviews tab to see the result."
                ),
                "type": "info",
                "sticky": False,
            },
        }

    def action_revalidate(self):
        self.ensure_one()
        if not (self.enriched_prompt or "").strip():
            raise UserError(_(
                "No enriched prompt to validate. Run Enrich Prompt first."
            ))
        from ..services import validator as validator_svc
        report = validator_svc.validate(
            self.enriched_prompt,
            style=self.style or "precise",
            category=self.category,
        )
        bucket = validator_svc.categorize(report)

        latest = self.enrichment_ids.sorted("attempt_number", reverse=True)[:1]
        if latest:
            latest.sudo().write({
                "validator_passed": report.passed,
                "validator_warnings": validator_svc.serialize_findings(report.warnings) or "",
                "validator_fatals": validator_svc.serialize_findings(report.fatal) or "",
                "word_count": report.word_count,
            })

        if bucket == "clean":
            self.write({"golden_prompt": self.enriched_prompt})
            msg = _("QC PASS — Golden Prompt set. %d words, 0 warnings.") % report.word_count
            ntype = "success"
            sticky = False
        elif bucket == "warned":
            self.write({"golden_prompt": False})
            rules = ", ".join(f.rule for f in report.warnings[:5])
            msg = _(
                "QC WARNED — not Golden. %(count)d warning(s): %(rules)s"
            ) % {"count": len(report.warnings), "rules": rules}
            ntype = "warning"
            sticky = True
        else:
            self.write({"golden_prompt": False})
            rules = ", ".join(f.rule for f in report.fatal[:5])
            msg = _(
                "QC FAILED — not Golden. %(count)d fatal(s): %(rules)s"
            ) % {"count": len(report.fatal), "rules": rules}
            ntype = "danger"
            sticky = True
        self.message_post(body=msg)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("QC Result"),
                "message": msg,
                "type": ntype,
                "sticky": sticky,
            },
        }

    def action_regenerate_with_failure(self):
        self.ensure_one()
        latest_fatal = self.enrichment_ids.filtered(
            lambda r: r.state == "fatal"
        ).sorted("attempt_number", reverse=True)[:1]
        if not latest_fatal:
            raise UserError(_(
                "No FATAL enrichment to regenerate from. "
                "Use Enrich Prompt for a fresh run."
            ))
        if self.last_enrichment_state in ("queued", "submitting", "validating"):
            raise UserError(_(
                "An enrichment is already in flight."
            ))
        icp = self.env["ir.config_parameter"].sudo()
        try:
            max_attempts = int(
                icp.get_param("t2av.enrichment_max_attempts", "3")
            )
        except (TypeError, ValueError):
            max_attempts = 3
        if self.enrichments_used >= max_attempts:
            raise UserError(_(
                "Maximum %(max)d enrichment attempts already reached. "
                "Edit prompt or metadata to invalidate the chain and start fresh."
            ) % {"max": max_attempts})
        access_key = credential_manager.get_aws_access_key(self.env)
        secret_key = credential_manager.get_aws_secret_key(self.env)
        if not access_key or not secret_key:
            raise UserError(_(
                "AWS Access Key / Secret Key not configured. "
                "Set them in Settings > T2AV."
            ))
        next_n = (max(self.enrichment_ids.mapped("attempt_number") or [0])) + 1
        retry = self.env["t2av.enrichment"].create({
            "job_id": self.id,
            "attempt_number": next_n,
            "state": "queued",
        })
        self.message_post(body=_(
            "Manual regenerate-with-failure queued (attempt #%(n)d / max %(max)d)."
        ) % {"n": next_n, "max": max_attempts})
        retry._defer("_run_enrich")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("T2AV"),
                "message": _(
                    "Regeneration queued. Validator findings from the latest "
                    "fatal attempt will be fed to the LLM. Refresh the form "
                    "or open the Enrichments tab to see the result."
                ),
                "type": "info",
                "sticky": False,
            },
        }

    def action_enrich(self):
        self.ensure_one()
        if not (self.prompt or "").strip():
            raise UserError(_("Prompt is required before enrichment."))
        if self.last_enrichment_state in ("queued", "submitting", "validating"):
            raise UserError(_(
                "An enrichment for this job is already in flight."
            ))
        icp = self.env["ir.config_parameter"].sudo()
        try:
            max_attempts = int(
                icp.get_param("t2av.enrichment_max_attempts", "3")
            )
        except (TypeError, ValueError):
            max_attempts = 3
        if self.enrichments_used >= max_attempts:
            raise UserError(_(
                "Maximum %d enrichment attempts reached for %s. "
                "Edit the prompt or metadata to start a fresh row."
            ) % (max_attempts, self.display_name))

        access_key = credential_manager.get_aws_access_key(self.env)
        secret_key = credential_manager.get_aws_secret_key(self.env)
        # if not access_key or not secret_key:
        #     raise UserError(_(
        #         "AWS Access Key / Secret Key not configured. "
        #         "Set them in Settings > T2AV > AWS Credentials."
        #     ))

        next_n = (max(self.enrichment_ids.mapped("attempt_number") or [0])) + 1
        enrichment = self.env["t2av.enrichment"].create({
            "job_id": self.id,
            "attempt_number": next_n,
            "state": "queued",
        })
        self.message_post(body=_("Enrichment #%d queued.") % next_n)
        enrichment._defer("_run_enrich")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("T2AV"),
                "message": _(
                    "Enrichment queued. Refresh the form or open the "
                    "Enrichments tab to see the result."
                ),
                "type": "info",
                "sticky": False,
            },
        }

    def action_start_retry(self):
        """Unlock the input fields so the user can edit before submitting
        attempt N+1. View toggles editability on ``ui_retry_pending``.
        """
        self.ensure_one()
        if self.state not in (_STATE_DONE, _STATE_FAILED):
            raise UserError(_(
                "Retry is only available after a Done or Failed attempt."
            ))
        if self.attempts_used >= MAX_ATTEMPTS:
            raise UserError(_("Max %d attempts reached.") % MAX_ATTEMPTS)
        self.write({"ui_retry_pending": True})
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_discard_retry(self):
        """Revert input fields to the latest attempt's values and lock editing."""
        self.ensure_one()
        last = self.attempt_ids.sorted("attempt_number", reverse=True)[:1]
        vals = {"ui_retry_pending": False}
        if last:
            vals.update({
                "prompt": last.prompt,
                "negative_prompt": last.negative_prompt or False,
                "duration": last.duration,
                "resolution": last.resolution,
                "aspect_ratio": last.aspect_ratio,
                "seed": last.seed or 0,
                "generate_audio": last.generate_audio,
            })
        self.write(vals)
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_submit_retry(self):
        """Spawn attempt N+1 with the (edited) input fields.

        Enforces: at least one input field must differ from the prior
        attempt (otherwise the user clicked Retry by mistake).
        """
        self.ensure_one()
        if not self.ui_retry_pending:
            raise UserError(_(
                "Click Retry first to start editing the inputs."
            ))
        if self.attempts_used >= MAX_ATTEMPTS:
            raise UserError(_("Max %d attempts reached.") % MAX_ATTEMPTS)
        diff = self._diff_vs_last_attempt()
        if not diff:
            last_n = self.attempt_ids.sorted(
                "attempt_number", reverse=True,
            )[:1].attempt_number
            raise UserError(_(
                "You must change at least one field before retrying. "
                "Current values are identical to attempt %d."
            ) % (last_n or 0))
        self._validate_can_submit()
        self.write({"ui_retry_pending": False})
        attempt = self._spawn_attempt()
        self.message_post(body=_(
            "Retry queued (attempt %(n)d). Changes: %(diff)s"
        ) % {
            "n": attempt.attempt_number,
            "diff": "; ".join(diff.keys()),
        })
        attempt._defer("_run_submit")
        return self._display_queued_notification()

    def _diff_vs_last_attempt(self):
        """Return a dict {field_name: (old, new)} of fields that changed
        between the job's current input values and the latest attempt.
        Empty dict if nothing changed (or no prior attempt).
        """
        self.ensure_one()
        last = self.attempt_ids.sorted("attempt_number", reverse=True)[:1]
        if not last:
            return {}
        fields_to_check = (
            "prompt", "negative_prompt", "duration",
            "resolution", "aspect_ratio", "seed", "generate_audio",
        )
        diff = {}
        for f in fields_to_check:
            new_val = getattr(self, f) or False
            old_val = getattr(last, f) or False
            if new_val != old_val:
                diff[f] = (old_val, new_val)
        return diff

    def _validate_can_submit(self):
        """Validate that the job has the credentials / config needed to
        actually run an OpenRouter submission. Raises ``UserError`` if not.
        """
        self.ensure_one()
        if not self.category:
            raise UserError(_(
                "Category is required before generating. "
                "Pick one of the 5 approved slugs."
            ))
        if not (self.prompt or "").strip():
            raise UserError(_("Prompt is required."))
        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            raise UserError(_(
                "OpenRouter API key is not configured. "
                "Set it in Settings > T2AV."
            ))
        icp = self.env["ir.config_parameter"].sudo()
        try:
            connector_id = int(icp.get_param("t2av.s3_connector_id") or 0)
        except (ValueError, TypeError):
            connector_id = 0
        if not connector_id:
            raise UserError(_(
                "S3 connector is not configured. "
                "Set it in Settings > T2AV."
            ))

    def action_cancel(self):
        """Cancel the active in-flight attempt."""
        self.ensure_one()
        a = self.active_attempt_id
        if not a or a.state not in _NON_TERMINAL:
            raise UserError(_("No in-flight attempt to cancel."))
        a.write({"state": _STATE_CANCELLED})
        if a.openrouter_job_id:
            try:
                api_key = credential_manager.get_openrouter_api_key(self.env)
                if api_key:
                    from ..services import openrouter_client
                    openrouter_client.cancel_job(api_key, a.openrouter_job_id)
            except Exception:
                _logger.exception(
                    "T2AV: cancel_job best-effort failed for attempt %s",
                    a.id,
                )
        self.message_post(body=_("Attempt %d cancelled.") % a.attempt_number)
        return True

    def action_reconcile(self):
        """Force-poll the active attempt synchronously.

        Useful after a worker restart left an attempt with no in-flight
        thread but the OpenRouter job is still running remotely. Calls
        the attempt's ``_run_poll`` directly (not via _defer) so the
        state transitions land on the current cursor.
        """
        self.ensure_one()
        a = self.active_attempt_id
        if not a or a.state not in _NON_TERMINAL:
            raise UserError(_("No in-flight attempt to reconcile."))
        a._run_poll()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_open_video(self):
        """Open the active done attempt's video URL in a new tab."""
        self.ensure_one()
        a = self.active_attempt_id
        if not a or a.state != _STATE_DONE:
            raise UserError(_("No completed attempt to open."))
        url = a.video_play_url or a.video_s3_url
        if not url:
            raise UserError(_("Video URL not available."))
        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "new",
        }

    def action_download(self):
        """Download the active attempt's MP4 via its ir.attachment.

        Falls back to ``action_open_video`` if no attachment was created
        (older successful attempts may not have one yet).
        """
        self.ensure_one()
        a = self.active_attempt_id
        if not a or a.state != _STATE_DONE:
            raise UserError(_("No completed attempt to download."))
        if a.video_attachment_id:
            return {
                "type": "ir.actions.act_url",
                "url": f"/web/content/{a.video_attachment_id.id}?download=true",
                "target": "self",
            }
        return self.action_open_video()

    def _display_queued_notification(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("T2AV"),
                "message": _("Generation queued."),
                "type": "info",
                "sticky": False,
            },
        }
