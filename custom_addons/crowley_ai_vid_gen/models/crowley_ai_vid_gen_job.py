# -*- coding: utf-8 -*-
"""Crowley AI video-generation job.

A job represents one creative goal owned by one user. The user iterates
toward that goal through up to **three attempts**: the original
generation (#1) plus up to two **refinements** where they revise the
prompt to fix something they didn't like in the previous result.

The job record carries the request parameters the user picks once
(model, duration, resolution, aspect ratio, audio, seed). Every actual
generation — its OpenRouter submission, its S3 object, its prompt log,
its lifecycle state — lives on a child ``crowley.ai.vid.gen.attempt``
row. The job exposes the latest attempt's lifecycle fields as stored
computes so views, the studio dashboard, and the JSON-RPC reel work
without knowing the schema split.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


MAX_ATTEMPTS = 3
_WORKING_STATES = ("queued", "submitting", "polling", "downloading")


class CrowleyAiVidGenJob(models.Model):
    _name = "crowley.ai.vid.gen.job"
    _description = "Crowley AI Video Generation Job (Seedance 2.0)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
        tracking=True,
    )

    prompt = fields.Text(
        required=True,
        tracking=True,
        help="The user's most-recent prompt. Each attempt also keeps its own "
             "historical prompt — see the Attempts tab.",
    )
    model = fields.Selection(
        [
            ("bytedance/seedance-2.0", "Seedance 2.0"),
            ("bytedance/seedance-2.0-fast", "Seedance 2.0 Fast"),
        ],
        default="bytedance/seedance-2.0",
        required=True,
        tracking=True,
    )
    duration = fields.Integer(
        default=15,
        tracking=True,
        help="Clip length in seconds (4..15). Default targets the Seedance maximum.",
    )
    resolution = fields.Selection(
        [("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        default="720p",
        required=True,
        tracking=True,
    )
    aspect_ratio = fields.Selection(
        [
            ("16:9", "16:9"),
            ("9:16", "9:16"),
            ("1:1", "1:1"),
            ("4:3", "4:3"),
            ("3:4", "3:4"),
            ("3:2", "3:2"),
            ("2:3", "2:3"),
            ("21:9", "21:9"),
        ],
        default="16:9",
        required=True,
        tracking=True,
    )
    generate_audio = fields.Boolean(default=True)
    seed = fields.Integer(
        help="Optional deterministic seed (0..2**31-1). Same seed is "
             "reused across all attempts of this job so the only thing "
             "varying between attempts is the prompt itself.",
    )

    user_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
    )

    attempt_ids = fields.One2many(
        "crowley.ai.vid.gen.attempt",
        "job_id",
        string="Attempts",
    )
    attempt_count = fields.Integer(
        compute="_compute_attempt_count",
        store=True,
    )
    active_attempt_id = fields.Many2one(
        "crowley.ai.vid.gen.attempt",
        compute="_compute_active_attempt_id",
        store=True,
        help="The most-recent attempt that produced a video, falling back "
             "to the most-recent attempt overall if none have completed.",
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("submitting", "Submitting"),
            ("polling", "Generating"),
            ("downloading", "Downloading"),
            ("ready", "Ready"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        compute="_compute_proxy_from_active_attempt",
        store=True,
        index=True,
        tracking=True,
        default="draft",
    )
    openrouter_job_id = fields.Char(
        compute="_compute_proxy_from_active_attempt",
        store=True,
        index=True,
    )
    openrouter_polling_url = fields.Char(
        compute="_compute_proxy_from_active_attempt",
        store=True,
    )
    poll_attempts = fields.Integer(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    last_poll_at = fields.Datetime(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    s3_bucket = fields.Char(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    s3_key = fields.Char(
        compute="_compute_proxy_from_active_attempt",
        store=True,
        index=True,
    )
    s3_etag = fields.Char(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    source_sha256 = fields.Char(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    file_size = fields.Integer(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    mimetype = fields.Char(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    cost_usd = fields.Float(
        compute="_compute_total_cost",
        store=True,
        digits=(10, 4),
        help="Sum of cost over every attempt in this job.",
    )
    error_message = fields.Text(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    submitted_at = fields.Datetime(
        compute="_compute_proxy_from_active_attempt", store=True,
    )
    completed_at = fields.Datetime(
        compute="_compute_proxy_from_active_attempt", store=True,
    )

    video_play_url = fields.Char(
        compute="_compute_video_play_url",
        store=False,
        readonly=True,
    )
    attachment_count = fields.Integer(
        string="Crowley Attachments",
        compute="_compute_attachment_count",
    )

    @api.depends("attempt_ids")
    def _compute_attempt_count(self):
        for rec in self:
            rec.attempt_count = len(rec.attempt_ids)

    @api.depends("attempt_ids", "attempt_ids.state", "attempt_ids.attempt_number")
    def _compute_active_attempt_id(self):
        """Pick the attempt the job's lifecycle fields should mirror.

        Prefers the latest *ready* attempt (so a successful refinement
        becomes the canvas video), falls back to the latest attempt of
        any state (so a failed attempt's error message surfaces on the
        job until the user kicks a new attempt).
        """
        for rec in self:
            ready = rec.attempt_ids.filtered(lambda a: a.state == "ready")
            if ready:
                rec.active_attempt_id = ready.sorted(
                    key=lambda a: a.attempt_number, reverse=True
                )[0]
            elif rec.attempt_ids:
                rec.active_attempt_id = rec.attempt_ids.sorted(
                    key=lambda a: a.attempt_number, reverse=True
                )[0]
            else:
                rec.active_attempt_id = False

    @api.depends(
        "active_attempt_id",
        "active_attempt_id.state",
        "active_attempt_id.openrouter_job_id",
        "active_attempt_id.openrouter_polling_url",
        "active_attempt_id.poll_attempts",
        "active_attempt_id.last_poll_at",
        "active_attempt_id.s3_bucket",
        "active_attempt_id.s3_key",
        "active_attempt_id.s3_etag",
        "active_attempt_id.source_sha256",
        "active_attempt_id.file_size",
        "active_attempt_id.mimetype",
        "active_attempt_id.error_message",
        "active_attempt_id.submitted_at",
        "active_attempt_id.completed_at",
    )
    def _compute_proxy_from_active_attempt(self):
        for rec in self:
            a = rec.active_attempt_id
            rec.state = a.state if a else "draft"
            rec.openrouter_job_id = a.openrouter_job_id if a else False
            rec.openrouter_polling_url = a.openrouter_polling_url if a else False
            rec.poll_attempts = a.poll_attempts if a else 0
            rec.last_poll_at = a.last_poll_at if a else False
            rec.s3_bucket = a.s3_bucket if a else False
            rec.s3_key = a.s3_key if a else False
            rec.s3_etag = a.s3_etag if a else False
            rec.source_sha256 = a.source_sha256 if a else False
            rec.file_size = a.file_size if a else 0
            rec.mimetype = a.mimetype if a else "video/mp4"
            rec.error_message = a.error_message if a else False
            rec.submitted_at = a.submitted_at if a else False
            rec.completed_at = a.completed_at if a else False

    @api.depends("attempt_ids.cost_usd")
    def _compute_total_cost(self):
        for rec in self:
            rec.cost_usd = sum(rec.attempt_ids.mapped("cost_usd")) or 0.0

    @api.depends(
        "active_attempt_id",
        "active_attempt_id.state",
        "active_attempt_id.s3_key",
        "active_attempt_id.mimetype",
    )
    def _compute_video_play_url(self):
        for rec in self:
            rec.video_play_url = (
                rec.active_attempt_id.video_play_url
                if rec.active_attempt_id
                else ""
            )

    def _compute_attachment_count(self):
        Attachment = self.env["ir.attachment"]
        for rec in self:
            rec.attachment_count = Attachment.search_count(
                [("crowley_job_id", "=", rec.id)]
            )

    @api.constrains("duration")
    def _check_duration(self):
        for rec in self:
            if not (4 <= rec.duration <= 15):
                raise ValidationError(
                    _("Duration must be between 4 and 15 seconds (got %s).") % rec.duration
                )

    @api.constrains("prompt")
    def _check_prompt(self):
        for rec in self:
            if not (rec.prompt or "").strip():
                raise ValidationError(_("Prompt cannot be empty."))
            if len(rec.prompt) > 4000:
                raise ValidationError(_("Prompt is too long (max 4000 chars)."))

    @api.constrains("seed")
    def _check_seed(self):
        for rec in self:
            if rec.seed and not (0 <= rec.seed < 2 ** 31):
                raise ValidationError(_("Seed must be a non-negative 32-bit integer."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("crowley.ai.vid.gen.job")
                    or _("New")
                )
        return super().create(vals_list)

    def action_generate(self):
        """Spawn attempt #1 for this job (the original generation).

        Multi-record safe. Each job is validated to be in a fresh state
        and gets exactly one attempt created. Refinements are produced
        via ``action_refine`` (which always increments the attempt
        counter beyond #1).
        """
        for rec in self:
            if rec.attempt_count >= 1:
                raise UserError(_(
                    "This job has already produced an attempt. Use 'Refine' to "
                    "submit a new attempt with a revised prompt."
                ))
            rec._spawn_attempt(rec.prompt)
        return True

    def action_refine(self, new_prompt=None):
        """Spawn the next attempt (#2 or #3) for this job.

        The user has watched a prior attempt's result and decided it
        needs adjustment. They've revised ``self.prompt`` (or we accept
        ``new_prompt`` as an override for programmatic callers). A new
        attempt row is created with the new prompt, the same seed, and
        the same job parameters; the new attempt runs through the full
        OpenRouter pipeline producing a fresh video that lands in the
        same S3 folder.
        """
        for rec in self:
            if rec.attempt_count >= MAX_ATTEMPTS:
                raise UserError(_(
                    "This job already used all %d allowed attempts. "
                    "Start a new job to keep iterating."
                ) % MAX_ATTEMPTS)
            in_flight = rec.attempt_ids.filtered(lambda a: a.state in _WORKING_STATES)
            if in_flight:
                raise UserError(_(
                    "Attempt %s is still in flight (state: %s). "
                    "Wait for it to finish before refining."
                ) % (in_flight[0].attempt_number, in_flight[0].state))
            prompt_for_attempt = (new_prompt or rec.prompt or "").strip()
            if not prompt_for_attempt:
                raise UserError(_("Cannot refine with an empty prompt."))
            if new_prompt and new_prompt.strip() != (rec.prompt or "").strip():
                rec.write({"prompt": new_prompt.strip()})
            rec._spawn_attempt(prompt_for_attempt)
        return True

    def _spawn_attempt(self, prompt_for_attempt):
        """Internal: create the next attempt row and defer its submit."""
        self.ensure_one()
        next_number = (self.attempt_count or 0) + 1
        attempt = self.env["crowley.ai.vid.gen.attempt"].create({
            "job_id": self.id,
            "attempt_number": next_number,
            "prompt": (prompt_for_attempt or "").strip(),
            "seed": self.seed or False,
            "state": "queued",
            "submitted_at": fields.Datetime.now(),
        })
        self.message_post(
            body=_("Submitting attempt %s to OpenRouter Seedance 2.0…") % next_number
        )
        attempt._defer("_run_submit")
        return attempt

    def action_cancel(self):
        for rec in self:
            target = rec.active_attempt_id
            if not target or target.state not in _WORKING_STATES:
                raise UserError(_(
                    "Only in-flight attempts can be cancelled (current job state: %s)."
                ) % rec.state)
            target.write({"state": "cancelled"})
            rec.message_post(
                body=_("Attempt %s cancelled by user.") % target.attempt_number
            )
        return True

    def action_retry(self):
        for rec in self:
            if rec.state not in ("failed", "cancelled"):
                raise UserError(_("Retry is only available on failed or cancelled jobs."))
        return self.action_refine()

    def action_resume_download(self):
        """Re-fetch the previously-generated video without re-submitting.

        Used when OpenRouter produced the video but the local download
        failed (e.g. S3 credentials were missing). Delegates to the
        active attempt — the per-attempt method already implements the
        24-hour content-URL recovery logic.
        """
        self.ensure_one()
        if not self.active_attempt_id:
            raise UserError(_("This job has no attempts yet."))
        return self.active_attempt_id.action_resume_download()

    def action_refresh_url(self):
        self.ensure_one()
        if self.active_attempt_id:
            self.active_attempt_id.invalidate_recordset(["video_play_url"])
        self.invalidate_recordset(["video_play_url"])
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_download(self):
        self.ensure_one()
        if not self.active_attempt_id or not self.active_attempt_id.s3_key:
            raise UserError(_("No video to download — this job hasn't produced a ready attempt."))
        return {
            "type": "ir.actions.act_url",
            "url": f"/crowley/seedance/video/{self.id}/download",
            "target": "self",
        }

    def action_view_attachments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Attachments"),
            "res_model": "ir.attachment",
            "view_mode": "list,form",
            "domain": [("crowley_job_id", "=", self.id)],
            "context": {"default_crowley_job_id": self.id},
        }

    def action_view_attempts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Attempts of %s") % (self.name or ""),
            "res_model": "crowley.ai.vid.gen.attempt",
            "view_mode": "list,form",
            "domain": [("job_id", "=", self.id)],
            "context": {"default_job_id": self.id},
        }

    @api.model
    def _cron_poll_openrouter(self):
        """Backwards-compatible shim — the cron's ``model_id`` may still
        point at this model on a freshly-upgraded database whose XML
        ``noupdate=1`` blocked the data file from re-applying. Forward
        to the attempt model so the cron keeps working until the
        post-init hook gets a chance to repoint it.
        """
        return self.env["crowley.ai.vid.gen.attempt"]._cron_poll_openrouter()

    def _build_callback_url(self, ICP):
        """Resolve the webhook URL OpenRouter should call us back on.

        Only ship a callback when a secret is configured — otherwise
        signature verification is impossible and we fall back to cron
        polling. The webhook controller looks up an *attempt* by
        ``openrouter_job_id`` (not the job), so the URL has no job/
        attempt path component.
        """
        self.ensure_one()
        secret = ICP.get_param("crowley_ai_vid_gen.webhook_secret", "")
        if not secret:
            return None
        explicit = ICP.get_param("crowley_ai_vid_gen.openrouter_callback_url", "")
        if explicit:
            return explicit
        base = ICP.get_param("web.base.url", "")
        if not base:
            return None
        return base.rstrip("/") + "/crowley/seedance/webhook"

    def _build_effective_prompt_for(self, ICP, user_prompt):
        """Prepend the admin-configured system prompt to ``user_prompt``.

        Called by each attempt's ``_run_submit`` so the per-attempt
        prompt log captures the exact bytes sent to OpenRouter. Empty/
        whitespace setting falls back to the user prompt verbatim.
        """
        self.ensure_one()
        prefix = (ICP.get_param("crowley_ai_vid_gen.system_prompt", "") or "").strip()
        user_prompt = (user_prompt or "").strip()
        if not prefix:
            return user_prompt
        return f"{prefix}\n\n{user_prompt}"
