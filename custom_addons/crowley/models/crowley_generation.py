"""Crowley video-generation job — the parent of one-to-three attempts.

A *crowley.generation* row represents one creative goal owned by one
user. The user iterates toward that goal through up to **three
attempts**: the original generation (#1) plus up to two **refinements**
where they revised one or more input fields to fix something they
didn't like in the previous result. Each attempt is a full OpenRouter
submission with its own state, costs, and S3 object — the job
aggregates them.

This module owns the *job* model only. The pipeline (submit / poll /
download) lives on ``crowley.attempt`` (see ``crowley_attempt.py``);
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
# The state machine itself lives on ``crowley.attempt``; the job's ``state``
# is a stored compute that mirrors the active attempt.
# ---------------------------------------------------------------------------
_STATE_DRAFT = "draft"
_STATE_QUEUED = "queued"
_STATE_SUBMITTING = "submitting"
_STATE_PROCESSING = "processing"
_STATE_DOWNLOADING = "downloading"
_STATE_DONE = "done"
_STATE_FAILED = "failed"
_STATE_CANCELLED = "cancelled"

# States that count as "this prompt is already taken" for duplicate detection:
# everything currently in flight (queued/submitting/processing/downloading) plus
# the completed result (done). Failed/cancelled are excluded so users can retry
# after a real failure.
_BLOCKING_FOR_DEDUP = (
    _STATE_QUEUED,
    _STATE_SUBMITTING,
    _STATE_PROCESSING,
    _STATE_DOWNLOADING,
    _STATE_DONE,
)

_NON_TERMINAL = {
    _STATE_QUEUED,
    _STATE_SUBMITTING,
    _STATE_PROCESSING,
    _STATE_DOWNLOADING,
}
_TERMINAL = {_STATE_DONE, _STATE_FAILED, _STATE_CANCELLED}

MAX_ATTEMPTS = 3

CATEGORY_SELECTION = [
    ("animals_wildlife", "Animals Wildlife"),
    ("animated_styles", "Animated Styles"),
    ("animated_text", "Animated Text"),
    ("av_sync_sound_effects", "AV Sync Sound Effects"),
    ("camera_motion", "Camera Motion"),
    ("educational_videos", "Educational Videos"),
    ("fantasy_surreal", "Fantasy Surreal"),
    ("fine_grained_motion", "Fine Grained Motion"),
    ("high_motion_action", "High Motion Action"),
    ("human_activities", "Human Activities"),
    ("multi_speaker_dialogue", "Multi-Speaker Dialogue"),
    ("music_performance", "Music Performance"),
    ("narrative_cinematic", "Narrative Cinematic"),
    ("natural_patterns", "Natural Patterns"),
    ("nature_weather", "Nature Weather"),
    ("person_emoting", "Person Emoting"),
    ("speech_styles", "Speech Styles"),
    ("urban_scenes", "Urban Scenes"),
    ("vehicles_machines", "Vehicles Machines"),
]

STYLE_SELECTION = [
    ("casual", "Casual"),
    ("creative", "Creative"),
    ("exhaustive", "Exhaustive"),
    ("narrative", "Narrative"),
    ("precise", "Precise"),
    ("terse", "Terse"),
]

PRIORITY_SELECTION = [
    ("high", "High"),
    ("highest", "Highest"),
    ("medium", "Medium"),
]

COMPLEXITY_SELECTION = [
    ("complex", "Complex"),
    ("lower", "Lower"),
    ("moderate", "Moderate"),
    ("simple", "Simple"),
]

LANGUAGE_SELECTION = [
    ("english", "English"),
]

CONTAINS_DIALOGUE_SELECTION = [
    ("true", "True"),
    ("false", "False"),
]

SPEAKER_COUNT_SELECTION = [
    ("0", "0"),
    ("1", "1"),
    ("2", "2"),
    ("3", "3"),
    ("4", "4"),
    ("5", "5"),
]


class CrowleyGeneration(models.Model):
    _name = "crowley.generation"
    _description = "Crowley Video Generation Job"
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
    original_prompt = fields.Text(
        string="Original Prompt",
        copy=True,
        help="Reference copy of the original prompt before enrichment.",
    )
    prompt = fields.Text(
        string="Video Generation Prompt",
        required=True,
        tracking=True,
    )
    negative_prompt = fields.Text(string="Negative Prompt")
    duration = fields.Selection(
        [
            ("4", "4s"),
            ("5", "5s"),
            ("6", "6s"),
            ("7", "7s"),
            ("8", "8s"),
            ("9", "9s"),
            ("10", "10s"),
            ("11", "11s"),
            ("12", "12s"),
            ("13", "13s"),
            ("14", "14s"),
            ("15", "15s"),
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
            ("16:9", "16:9"),
            ("9:16", "9:16"),
            ("1:1", "1:1"),
            ("4:3", "4:3"),
            ("3:4", "3:4"),
            ("21:9", "21:9"),
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
    # Content annotation fields (v1.4)
    # ------------------------------------------------------------------
    sub_category = fields.Text(string="Sub-Category", copy=True)
    topic = fields.Text(string="Topic", copy=True)
    style = fields.Selection(STYLE_SELECTION, string="Style")
    priority = fields.Selection(PRIORITY_SELECTION, string="Priority")
    complexity = fields.Selection(COMPLEXITY_SELECTION, string="Complexity")
    language = fields.Selection(
        LANGUAGE_SELECTION, string="Language", default="english"
    )
    contains_dialogue = fields.Selection(
        CONTAINS_DIALOGUE_SELECTION, string="Contains Dialogue"
    )
    speaker_count = fields.Selection(SPEAKER_COUNT_SELECTION, string="Speaker Count")

    # ------------------------------------------------------------------
    # Attempt linkage — the new core
    # ------------------------------------------------------------------
    attempt_ids = fields.One2many(
        "crowley.attempt",
        inverse_name="job_id",
        string="Attempts",
    )
    active_attempt_id = fields.Many2one(
        "crowley.attempt",
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
    allow_duplicate = fields.Boolean(
        string="Allow Duplicate Prompt",
        default=False,
        copy=False,
        tracking=True,
        groups="crowley.group_crowley_manager",
        help="Manager-only override: bypass the duplicate-prompt check for this "
        "generation. Use sparingly — e.g. regenerating after a model upgrade, "
        "A/B testing, or replacing a rejected video with a fresh take.",
    )

    # ------------------------------------------------------------------
    # State proxy — computed from active attempt, stored for list views
    # ------------------------------------------------------------------
    state = fields.Selection(
        [
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
    # Review proxy (mirrors active_attempt_id.review_state)
    # ------------------------------------------------------------------
    review_state = fields.Selection(
        [
            ("pending", "Pending Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Review Status",
        compute="_compute_review_state",
        store=True,
        index=True,
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
        return self.env["ir.sequence"].next_by_code("crowley.generation") or _("New")

    def _default_resolution(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("crowley.default_resolution", "720p")
        )

    def _default_duration(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("crowley.default_duration", "5")
        )

    def _default_model_name(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("crowley.default_model", "bytedance/seedance-2.0")
        )

    # ------------------------------------------------------------------
    # Compute methods
    # ------------------------------------------------------------------
    @api.depends("attempt_ids", "attempt_ids.state", "attempt_ids.attempt_number")
    def _compute_active_attempt(self):
        """Active attempt priority: in-progress > latest done > latest by number."""
        for rec in self:
            in_progress = rec.attempt_ids.filtered(
                lambda a: a.state in _NON_TERMINAL
            ).sorted("attempt_number", reverse=True)
            if in_progress:
                rec.active_attempt_id = in_progress[:1]
                continue
            done = rec.attempt_ids.filtered(lambda a: a.state == _STATE_DONE).sorted(
                "attempt_number", reverse=True
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
                    rec.attempts_used,
                    MAX_ATTEMPTS,
                )
            else:
                rec.attempts_label = _("Attempt %d/%d") % (
                    rec.attempts_used,
                    MAX_ATTEMPTS,
                )

    @api.depends("attempt_ids.state")
    def _compute_category_locked(self):
        for rec in self:
            rec.category_locked = any(a.state == "done" for a in rec.attempt_ids)

    @api.depends("active_attempt_id", "active_attempt_id.state")
    def _compute_state(self):
        for rec in self:
            rec.state = (
                rec.active_attempt_id.state if rec.active_attempt_id else _STATE_DRAFT
            )

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

    @api.depends("attempt_ids.review_state")
    def _compute_review_state(self):
        for rec in self:
            states = set(rec.attempt_ids.mapped("review_state")) - {False}
            if "approved" in states:
                rec.review_state = "approved"
            elif states and states == {"rejected"}:
                rec.review_state = "rejected"
            elif "pending" in states:
                rec.review_state = "pending"
            else:
                rec.review_state = False

    def action_approve(self):
        self.ensure_one()
        attempt = self.active_attempt_id
        if not attempt:
            raise UserError(_("No active attempt to approve."))
        return attempt.action_approve()

    def action_reject(self):
        self.ensure_one()
        attempt = self.active_attempt_id
        if not attempt:
            raise UserError(_("No active attempt to reject."))
        return attempt.action_reject()

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

    @api.constrains("original_prompt")
    def _check_original_prompt(self):
        for rec in self:
            stripped = (rec.original_prompt or "").strip()
            if stripped and len(stripped) > 2000:
                raise ValidationError(
                    _("Original Prompt must be at most 2000 characters.")
                )

    @api.constrains("seed")
    def _check_seed(self):
        for rec in self:
            if rec.seed < 0 or rec.seed > 2_147_483_647:
                raise ValidationError(_("Seed must be between 0 and 2,147,483,647."))

    # ------------------------------------------------------------------
    # CRUD overrides — only the unlink guard remains. State is now a
    # computed proxy, so the v1 write() guard moved onto the attempt.
    # ------------------------------------------------------------------
    def unlink(self):
        for rec in self:
            if rec.state in _NON_TERMINAL:
                raise UserError(
                    _("Cannot delete %s while an attempt is in progress.")
                    % rec.display_name
                )
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
            raise UserError(
                _("Max %(max)d attempts reached for %(name)s. Create a new generation.")
                % {"max": MAX_ATTEMPTS, "name": self.display_name}
            )
        next_n = (max(self.attempt_ids.mapped("attempt_number") or [0])) + 1
        attempt = self.env["crowley.attempt"].create(
            {
                "job_id": self.id,
                "attempt_number": next_n,
                "prompt": self.prompt,
                "original_prompt": self.original_prompt or False,
                "negative_prompt": self.negative_prompt or False,
                "duration": self.duration,
                "resolution": self.resolution,
                "aspect_ratio": self.aspect_ratio,
                "seed": self.seed or 0,
                "generate_audio": self.generate_audio,
                "model_name": self.model_name or "bytedance/seedance-2.0",
                "state": _STATE_QUEUED,
            }
        )
        return attempt

    # ------------------------------------------------------------------
    # Action methods — the centerpiece UX
    # ------------------------------------------------------------------
    def action_generate(self):
        """Spawn attempt #1. Guards: no existing attempts on this job."""
        self.ensure_one()
        if self.attempt_ids:
            raise UserError(
                _("Use Retry to start another attempt — this job already has attempts.")
            )
        self._validate_can_submit()
        attempt = self._spawn_attempt()
        self.message_post(body=_("Generation queued (attempt 1)."))
        attempt._defer("_run_submit")
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_start_retry(self):
        """Unlock the input fields so the user can edit before submitting
        attempt N+1. View toggles editability on ``ui_retry_pending``.
        """
        self.ensure_one()
        if self.state not in (_STATE_DONE, _STATE_FAILED):
            raise UserError(
                _("Retry is only available after a Done or Failed attempt.")
            )
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
            vals.update(
                {
                    "prompt": last.prompt,
                    "negative_prompt": last.negative_prompt or False,
                    "duration": last.duration,
                    "resolution": last.resolution,
                    "aspect_ratio": last.aspect_ratio,
                    "seed": last.seed or 0,
                    "generate_audio": last.generate_audio,
                }
            )
        self.write(vals)
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_submit_retry(self):
        """Spawn attempt N+1 with the (edited) input fields.

        Enforces: at least one input field must differ from the prior
        attempt (otherwise the user clicked Retry by mistake).
        """
        self.ensure_one()
        if not self.ui_retry_pending:
            raise UserError(_("Click Retry first to start editing the inputs."))
        if self.attempts_used >= MAX_ATTEMPTS:
            raise UserError(_("Max %d attempts reached.") % MAX_ATTEMPTS)
        diff = self._diff_vs_last_attempt()
        if not diff:
            last_n = self.attempt_ids.sorted(
                "attempt_number",
                reverse=True,
            )[:1].attempt_number
            raise UserError(
                _(
                    "You must change at least one field before retrying. "
                    "Current values are identical to attempt %d."
                )
                % (last_n or 0)
            )
        self._validate_can_submit()
        self.write({"ui_retry_pending": False})
        attempt = self._spawn_attempt()
        self.message_post(
            body=_("Retry queued (attempt %(n)d). Changes: %(diff)s")
            % {
                "n": attempt.attempt_number,
                "diff": "; ".join(diff.keys()),
            }
        )
        attempt._defer("_run_submit")
        return {"type": "ir.actions.client", "tag": "reload"}

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
            "prompt",
            "negative_prompt",
            "duration",
            "resolution",
            "aspect_ratio",
            "seed",
            "generate_audio",
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
            raise UserError(
                _(
                    "Category is required before generating. "
                    "Pick one of the 5 approved slugs."
                )
            )
        if not (self.prompt or "").strip():
            raise UserError(_("Prompt is required."))
        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            raise UserError(
                _("OpenRouter API key is not configured. Set it in Settings > Crowley.")
            )
        icp = self.env["ir.config_parameter"].sudo()
        try:
            connector_id = int(icp.get_param("crowley.s3_connector_id") or 0)
        except (ValueError, TypeError):
            connector_id = 0
        if not connector_id:
            raise UserError(
                _("S3 connector is not configured. Set it in Settings > Crowley.")
            )
        self._check_duplicate_prompts()

    def _check_duplicate_prompts(self):
        """Block submission if any done attempt (any user, any job) already has this prompt.

        Uses sudo() so the check spans the whole dataset — duplicate prevention
        is org-wide, not user-scoped. Skipped when allow_duplicate=True
        (manager-only override). Empty/whitespace-only inputs normalize to
        False and are not checked.
        """
        self.ensure_one()
        if self.sudo().allow_duplicate:
            return
        Attempt = self.env["crowley.attempt"]
        norm_prompt = Attempt._normalize_prompt_text(self.prompt)
        norm_original = Attempt._normalize_prompt_text(self.original_prompt)
        if not norm_prompt and not norm_original:
            return

        base_domain = [
            ("state", "in", list(_BLOCKING_FOR_DEDUP)),
            ("job_id", "!=", self.id or 0),
        ]
        AttemptSudo = Attempt.sudo()
        matches = AttemptSudo.browse()
        if norm_prompt:
            matches = AttemptSudo.search(
                base_domain + [("prompt_normalized", "=", norm_prompt)],
                limit=1,
            )
        if not matches and norm_original:
            matches = AttemptSudo.search(
                base_domain + [("original_prompt_normalized", "=", norm_original)],
                limit=1,
            )

        if not matches:
            return

        conflict = matches[0]
        conflict_job = conflict.job_id
        user_name = conflict_job.user_id.display_name or _("—")

        if self.id:
            self.message_post(
                body=_(
                    "Duplicate-prompt check matched %(name)s/attempt #%(num)d "
                    "(by %(user)s). Submission blocked.",
                    name=conflict_job.name,
                    num=conflict.attempt_number,
                    user=user_name,
                )
            )

        raise UserError(
            _(
                "Duplicate prompt detected.\n\n"
                "A video for this prompt already exists or is being generated:\n"
                "  • Generation: %(name)s (created %(date)s)\n"
                "  • Attempt: #%(num)d, state: %(state)s, review: %(review)s\n"
                "  • By user: %(user)s\n\n"
                "Choose a different prompt, or ask a Manager to enable the "
                "'Allow Duplicate Prompt' option on your generation.",
                name=conflict_job.name,
                date=fields.Datetime.to_string(conflict_job.create_date)
                if conflict_job.create_date
                else _("unknown"),
                num=conflict.attempt_number,
                state=dict(conflict._fields["state"].selection).get(
                    conflict.state,
                    conflict.state or _("—"),
                ),
                review=dict(conflict._fields["review_state"].selection).get(
                    conflict.review_state,
                    conflict.review_state or _("—"),
                ),
                user=user_name,
            )
        )

    @api.onchange("prompt", "original_prompt")
    def _onchange_prompts_dup_warning(self):
        if not (self.prompt or self.original_prompt):
            return
        if self.sudo().allow_duplicate:
            return
        try:
            self._check_duplicate_prompts()
        except UserError as e:
            return {"warning": {"title": _("Duplicate Prompt"), "message": str(e)}}

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
                    "Crowley: cancel_job best-effort failed for attempt %s",
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
                "title": _("Crowley"),
                "message": _("Generation queued."),
                "type": "info",
                "sticky": False,
            },
        }
