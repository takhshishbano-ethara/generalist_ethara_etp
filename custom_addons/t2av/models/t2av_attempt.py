"""T2AV video generation attempt — per-attempt state under a parent job.

A *t2av.generation* job owns up to **three** attempts: attempt #1 (the
original generation) plus up to two **refinements** where the user revised
the prompt to fix something they didn't like in the previous result. Every
attempt is a full OpenRouter submission with its own S3 object, cost, and
state machine — the parent job aggregates them and exposes the
``active_attempt_id`` for the form view.
"""

import json
import logging
import time
import traceback
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import openrouter_client, s3_publisher
from . import credential_manager

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine — mirrors v1 but moved onto the attempt
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Bus channel for OWL live-status widget
# ---------------------------------------------------------------------------
_BUS_CHANNEL = "t2av.generation"
_BUS_TYPE = "t2av.generation.update"

# Map OpenRouter `status` field to our internal state
_OR_STATUS_PROCESSING = {"pending", "queued", "in_progress", "processing", "running"}
_OR_STATUS_COMPLETED = {"completed", "succeeded", "success"}
_OR_STATUS_FAILED = {"failed", "error"}
_OR_STATUS_CANCELLED = {"cancelled"}
_OR_STATUS_EXPIRED = {"expired"}

_ALLOWED_TRANSITIONS = {
    # Lenient on entry transitions (worker race against postcommit), strict
    # on exit transitions.
    _STATE_DRAFT:        {_STATE_QUEUED, _STATE_SUBMITTING, _STATE_FAILED},
    _STATE_QUEUED:       {_STATE_SUBMITTING, _STATE_FAILED, _STATE_CANCELLED},
    _STATE_SUBMITTING:   {_STATE_PROCESSING, _STATE_FAILED, _STATE_CANCELLED},
    _STATE_PROCESSING:   {_STATE_DOWNLOADING, _STATE_FAILED, _STATE_CANCELLED, _STATE_PROCESSING},
    _STATE_DOWNLOADING:  {_STATE_DONE, _STATE_FAILED, _STATE_CANCELLED, _STATE_DOWNLOADING},
    _STATE_DONE:         set(),
    _STATE_FAILED:       set(),
    _STATE_CANCELLED:    set(),
}


class T2AVAttempt(models.Model):
    _name = "t2av.attempt"
    _description = "T2AV Video Generation Attempt"
    _inherit = ["mail.thread"]
    _order = "job_id, attempt_number"
    _rec_name = "display_name"

    # ------------------------------------------------------------------
    # Linkage
    # ------------------------------------------------------------------
    job_id = fields.Many2one(
        "t2av.generation",
        string="Job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    attempt_number = fields.Integer(
        string="Attempt #",
        required=True,
        copy=False,
        help="1 for the original generation; 2 or 3 for user-driven refinements.",
    )
    display_name = fields.Char(
        compute="_compute_display_name",
        store=False,
    )

    # ------------------------------------------------------------------
    # User inputs (copied from the job at spawn-time, immutable after submit)
    # ------------------------------------------------------------------
    prompt = fields.Text(
        string="Prompt",
        required=True,
        copy=False,
    )
    effective_prompt = fields.Text(
        string="Effective Prompt",
        readonly=True,
        copy=False,
        help="Exact prompt string sent to OpenRouter (system prefix + user prompt).",
    )
    negative_prompt = fields.Text(string="Negative Prompt")
    duration = fields.Selection(
        [
            ("4", "4s"), ("5", "5s"), ("6", "6s"), ("7", "7s"), ("8", "8s"),
            ("9", "9s"), ("10", "10s"), ("12", "12s"), ("15", "15s"),
        ],
        string="Duration",
    )
    resolution = fields.Selection(
        [("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        string="Resolution",
    )
    aspect_ratio = fields.Selection(
        [
            ("16:9", "16:9"), ("9:16", "9:16"), ("1:1", "1:1"),
            ("4:3", "4:3"), ("3:4", "3:4"), ("21:9", "21:9"),
        ],
        string="Aspect Ratio",
    )
    seed = fields.Integer(string="Seed", default=0)
    generate_audio = fields.Boolean(string="Generate Audio", default=True)
    model_name = fields.Char(string="Model")

    # ------------------------------------------------------------------
    # State
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
        default=_STATE_DRAFT,
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # OpenRouter tracking
    # ------------------------------------------------------------------
    openrouter_job_id = fields.Char(string="OpenRouter Job ID", index=True, copy=False)
    openrouter_polling_url = fields.Char(string="Polling URL", copy=False)
    openrouter_status = fields.Char(string="API Status", copy=False)
    poll_attempts = fields.Integer(string="Poll Attempts", default=0, copy=False)
    last_polled_at = fields.Datetime(string="Last Polled At", copy=False)

    # ------------------------------------------------------------------
    # Output / S3
    # ------------------------------------------------------------------
    video_temporary_url = fields.Char(string="Temporary URL", copy=False)
    video_expires_at = fields.Datetime(string="URL Expires At", copy=False)
    video_s3_bucket = fields.Char(string="S3 Bucket", readonly=True, copy=False)
    video_s3_key = fields.Char(string="S3 Key", index=True, readonly=True, copy=False)
    # Backward-compat with v1 data; new code should prefer ``video_play_url``.
    video_s3_url = fields.Char(string="S3 URL", readonly=True, copy=False)
    video_s3_etag = fields.Char(string="S3 ETag", readonly=True, copy=False)
    video_sha256 = fields.Char(string="SHA-256", readonly=True, copy=False)
    video_size_bytes = fields.Integer(string="Size (bytes)", readonly=True, copy=False)
    mimetype = fields.Char(string="MIME Type", default="video/mp4", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Dataset naming (v1.2)
    # ------------------------------------------------------------------
    category = fields.Char(
        string="Category",
        readonly=True, copy=False, index=True,
        help="Snapshot of the job's category at the moment this attempt was "
             "uploaded. Required for the canonical filename.",
    )
    sequence_number = fields.Integer(
        string="Dataset Sequence #",
        readonly=True, copy=False, index=True,
        help="Allocated from the per-category ir.sequence at successful S3 "
             "upload. Failed attempts never consume a number.",
    )
    video_file = fields.Char(
        string="Video File",
        compute="_compute_video_file",
        store=True, readonly=True, index=True,
        help="Canonical dataset filename: T2AV_<category>_<NNNNNNN>.mp4 (padding 7 since v19.0.1.14.0).",
    )
    fps = fields.Float(
        string="FPS",
        default=24.0,
        readonly=True,
        copy=False,
        help="Frames per second captured from the Seedance response when present; "
             "defaults to 24.0 (Seedance 2.0's standard output rate).",
    )

    video_play_url = fields.Char(
        string="Play URL",
        compute="_compute_video_play_url",
        store=False,
    )
    video_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Attachment",
        ondelete="set null",
    )

    # ------------------------------------------------------------------
    # Usage / cost
    # ------------------------------------------------------------------
    tokens_used = fields.Integer(string="Tokens Used", default=0, readonly=True, copy=False)
    cost_usd = fields.Float(
        string="Cost (USD)",
        digits=(12, 6),
        default=0.0,
        readonly=True,
        copy=False,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="job_id.currency_id",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    error_message = fields.Text(string="Error Message", readonly=True, copy=False)
    error_code = fields.Char(string="Error Code", readonly=True, copy=False)
    submitted_at = fields.Datetime(string="Submitted At", readonly=True, copy=False)
    completed_at = fields.Datetime(string="Completed At", readonly=True, copy=False)
    duration_seconds = fields.Float(
        string="Wall Time (s)",
        compute="_compute_duration_seconds",
        store=True,
    )
    raw_response_json = fields.Text(string="Last Raw Response")

    review_ids = fields.One2many(
        "t2av.video.review", "attempt_id", string="Reviews",
    )

    # ------------------------------------------------------------------
    # Webhook idempotency
    # ------------------------------------------------------------------
    webhook_idempotency_key = fields.Char(
        string="Webhook Idempotency Key",
        index=True,
        copy=False,
    )

    # ------------------------------------------------------------------
    # Change log (centerpiece UX field — diff vs prior attempt)
    # ------------------------------------------------------------------
    change_log = fields.Text(
        string="Change Log",
        compute="_compute_change_log",
        store=True,
    )

    # Odoo 19 replaced `_sql_constraints` with `models.Constraint` class attrs.
    _unique_per_job = models.Constraint(
        "UNIQUE(job_id, attempt_number)",
        message="Attempt number must be unique within a job.",
    )
    _number_range = models.Constraint(
        "CHECK(attempt_number BETWEEN 1 AND 3)",
        message="Attempt number must be between 1 and 3.",
    )
    _or_job_id_unique = models.Constraint(
        "UNIQUE(openrouter_job_id)",
        message="OpenRouter job ID must be unique across attempts.",
    )
    _poll_attempts_nonneg = models.Constraint(
        "CHECK(poll_attempts >= 0)",
        message="Poll attempts cannot be negative.",
    )
    _unique_video_file = models.Constraint(
        "UNIQUE(video_file)",
        message="Video filenames must be unique across the dataset.",
    )
    _unique_seq_per_category = models.Constraint(
        "UNIQUE(category, sequence_number)",
        message="Each (category, sequence_number) pair must be unique.",
    )

    # ------------------------------------------------------------------
    # Compute methods
    # ------------------------------------------------------------------
    @api.depends("job_id.name", "attempt_number")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = (
                f"{rec.job_id.name or 'CRW/?'} · attempt {rec.attempt_number or '?'}"
            )

    @api.depends("submitted_at", "completed_at")
    def _compute_duration_seconds(self):
        for rec in self:
            if rec.submitted_at and rec.completed_at:
                rec.duration_seconds = (rec.completed_at - rec.submitted_at).total_seconds()
            else:
                rec.duration_seconds = 0.0

    @api.depends("category", "sequence_number")
    def _compute_video_file(self):
        for rec in self:
            if rec.category and rec.sequence_number:
                rec.video_file = f"T2AV_{rec.category}_{rec.sequence_number:06d}.mp4"
            else:
                rec.video_file = False

    def _compute_video_play_url(self):
        """Re-generate a presigned URL on each read so it never expires from the user's view."""
        icp = self.env["ir.config_parameter"].sudo()
        ttl = int(icp.get_param("t2av.presigned_ttl_seconds", "300") or "300")
        try:
            connector_id = int(icp.get_param("t2av.s3_connector_id") or 0)
        except (ValueError, TypeError):
            connector_id = 0
        storage = self.env["t2av.s3.storage"]
        for rec in self:
            if not rec.video_s3_key or not connector_id:
                # Fall back to stored public URL if presigned can't be generated.
                rec.video_play_url = rec.video_s3_url or ""
                continue
            try:
                rec.video_play_url = storage.presigned_get_url(
                    connector_id,
                    rec.video_s3_key,
                    expires_in=ttl,
                    mimetype=rec.mimetype or "video/mp4",
                    disposition="inline",
                    filename=f"{rec.display_name}.mp4" if rec.display_name else None,
                )
            except Exception:
                _logger.exception(
                    "T2AV: failed to presign URL for attempt %s", rec.id,
                )
                rec.video_play_url = rec.video_s3_url or ""

    @api.depends(
        "job_id.attempt_ids.attempt_number", "attempt_number",
        "prompt", "duration", "resolution", "aspect_ratio",
        "negative_prompt", "seed", "generate_audio",
    )
    def _compute_change_log(self):
        """Human-readable diff vs the prior attempt."""
        for rec in self:
            if rec.attempt_number == 1:
                rec.change_log = "Initial attempt"
                continue
            prior_n = rec.attempt_number - 1
            prior = rec.job_id.attempt_ids.filtered(
                lambda a: a.attempt_number == prior_n
            )[:1]
            if not prior:
                rec.change_log = ""
                continue
            diffs = []
            for f, label in (
                ("prompt", "prompt"),
                ("duration", "duration"),
                ("resolution", "resolution"),
                ("aspect_ratio", "aspect"),
                ("seed", "seed"),
                ("negative_prompt", "negative_prompt"),
                ("generate_audio", "audio"),
            ):
                old, new = getattr(prior, f), getattr(rec, f)
                if old != new:
                    if f in ("prompt", "negative_prompt"):
                        diffs.append(f"{label} edited")
                    else:
                        diffs.append(f"{label}: {old} → {new}")
            rec.change_log = "; ".join(diffs) if diffs else "No input changes (retry only)"

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("attempt_number")
    def _check_attempt_number(self):
        for rec in self:
            if not (1 <= rec.attempt_number <= 3):
                raise ValidationError(_("Attempt number must be between 1 and 3."))

    @api.constrains("prompt")
    def _check_prompt(self):
        for rec in self:
            stripped = (rec.prompt or "").strip()
            if not stripped:
                raise ValidationError(_("Prompt is required."))
            if len(stripped) > 2000:
                raise ValidationError(_("Prompt must be at most 2000 characters."))

    # ------------------------------------------------------------------
    # CRUD / state-machine guard
    # ------------------------------------------------------------------
    def write(self, vals):
        if "state" in vals:
            new_state = vals["state"]
            for rec in self:
                old_state = rec.state
                if old_state == new_state:
                    continue
                allowed = _ALLOWED_TRANSITIONS.get(old_state, set())
                if new_state not in allowed:
                    raise ValidationError(_(
                        "Cannot transition attempt %(name)s from %(old)s to %(new)s."
                    ) % {
                        "name": rec.display_name,
                        "old": old_state,
                        "new": new_state,
                    })
        return super().write(vals)

    # ------------------------------------------------------------------
    # Post-commit helper (dual-cursor failure recovery)
    # ------------------------------------------------------------------
    def _defer(self, callback_name, *args):
        """Schedule a method to run after-commit on a fresh cursor.

        Dual-cursor failure recording: if the deferred call raises, the
        original cursor is rolled back and we open a SECOND cursor to
        persist the failure state — otherwise the failure write would also
        be rolled back and the attempt would be invisibly stuck.
        """
        import odoo

        db_name = self.env.cr.dbname
        rec_id = self.id
        uid = self.env.uid

        def _run():
            try:
                registry = odoo.modules.registry.Registry(db_name)
                with registry.cursor() as cr:
                    env = odoo.api.Environment(cr, uid, {})
                    attempt = env["t2av.attempt"].browse(rec_id).exists()
                    if attempt:
                        getattr(attempt, callback_name)(*args)
            except Exception:
                _logger.exception(
                    "T2AV deferred task failed (attempt %s, cb %s)",
                    rec_id, callback_name,
                )
                try:
                    registry = odoo.modules.registry.Registry(db_name)
                    with registry.cursor() as cr2:
                        env2 = odoo.api.Environment(cr2, uid, {})
                        attempt = env2["t2av.attempt"].browse(rec_id).exists()
                        if attempt:
                            attempt.write({
                                "state": _STATE_FAILED,
                                "error_code": "internal_error",
                                "error_message": (traceback.format_exc() or "")[:8000],
                            })
                except Exception:
                    _logger.exception(
                        "T2AV: failed to mark attempt %s as failed", rec_id,
                    )

        self.env.cr.postcommit.add(_run)

    # ------------------------------------------------------------------
    # Pipeline — Phase 1+2: submit to OpenRouter
    # ------------------------------------------------------------------
    def _run_submit(self):
        """Submit this attempt to OpenRouter. Called via _defer() postcommit."""
        self.ensure_one()
        # Skip if already past submitting (e.g., webhook beat us to it)
        if self.state not in (_STATE_DRAFT, _STATE_QUEUED):
            return

        self.write({"state": _STATE_SUBMITTING, "submitted_at": fields.Datetime.now()})
        self._push_bus()

        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            self._fail("auth", "OpenRouter API key not configured.")
            return

        settings = self._get_settings()

        try:
            resp = openrouter_client.submit_video(
                api_key,
                prompt=(self.prompt or "").strip(),
                duration=int(self.duration) if self.duration else 5,
                resolution=self.resolution or "720p",
                aspect_ratio=self.aspect_ratio or "16:9",
                negative_prompt=(self.negative_prompt or None),
                seed=(self.seed or None),
                generate_audio=bool(self.generate_audio),
                model=self.model_name or "bytedance/seedance-2.0",
                http_referer=settings["http_referer"] or None,
                app_title=settings["app_title"] or None,
            )
        except openrouter_client.OpenRouterAuthError as e:
            self._fail("auth", f"Invalid OpenRouter API key: {e}")
            return
        except openrouter_client.OpenRouterRateLimitError as e:
            self._fail("rate_limit", f"OpenRouter rate-limited: {e}")
            return
        except openrouter_client.OpenRouterValidationError as e:
            self._fail("validation", f"Validation error: {e}")
            return
        except openrouter_client.OpenRouterTimeoutError as e:
            self._fail("network", f"OpenRouter timeout: {e}")
            return
        except openrouter_client.OpenRouterAPIError as e:
            self._fail("api_error", f"OpenRouter error: {e}")
            return
        except openrouter_client.OpenRouterError as e:
            self._fail("openrouter", f"OpenRouter call failed: {e}")
            return

        job_id = resp.get("id")
        polling_url = resp.get("polling_url")
        if not job_id:
            self._fail("submit_no_id", "OpenRouter did not return a job id.")
            return

        self.write({
            "state": _STATE_PROCESSING,
            "openrouter_job_id": job_id,
            "openrouter_polling_url": polling_url or False,
            "openrouter_status": resp.get("status") or "pending",
        })
        self.message_post(body=_("Submitted to OpenRouter (job %s).") % job_id)
        self._push_bus()

    # ------------------------------------------------------------------
    # Pipeline — Phase 3: poll for completion
    # ------------------------------------------------------------------
    def _run_poll(self):
        """Single poll iteration. Called by cron or by reconcile button."""
        self.ensure_one()
        if self.state not in (_STATE_SUBMITTING, _STATE_PROCESSING):
            return
        if not self.openrouter_job_id:
            self._fail("orphaned", "Polling an attempt without an OpenRouter job id.")
            return

        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            self._fail("auth", "OpenRouter API key not configured.")
            return

        try:
            resp = openrouter_client.poll_status(
                api_key, self.openrouter_job_id,
                polling_url=self.openrouter_polling_url or None,
            )
        except openrouter_client.OpenRouterAuthError as e:
            self._fail("auth", f"Invalid OpenRouter API key during poll: {e}")
            return
        except openrouter_client.OpenRouterRateLimitError:
            # Don't fail on rate-limit; let the next cron tick retry
            return
        except openrouter_client.OpenRouterTimeoutError:
            # Same — transient
            return
        except openrouter_client.OpenRouterError as e:
            self._fail("poll_error", f"OpenRouter poll failed: {e}")
            return

        raw_status = (resp.get("status") or "").lower()
        self.write({
            "poll_attempts": self.poll_attempts + 1,
            "last_polled_at": fields.Datetime.now(),
            "openrouter_status": raw_status,
            "raw_response_json": json.dumps(resp)[:65536],
        })

        if raw_status in _OR_STATUS_FAILED or raw_status in _OR_STATUS_EXPIRED:
            err = self._extract_or_error(resp)
            self._fail("openrouter_failed", err)
            return
        if raw_status in _OR_STATUS_CANCELLED:
            self.write({"state": _STATE_CANCELLED})
            self._push_bus()
            return
        if raw_status in _OR_STATUS_COMPLETED:
            self._handle_completion(resp)
            return
        # Otherwise still processing — push bus so OWL widget updates poll counter
        self._push_bus()

    def _extract_or_error(self, resp):
        err = resp.get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        return err or resp.get("message") or "Unknown OpenRouter failure"

    # ------------------------------------------------------------------
    # Pipeline — completion handler (idempotent compare-and-set)
    # ------------------------------------------------------------------
    def _extract_fps_from_response(self, resp):
        if not isinstance(resp, dict):
            return 0.0
        sources = (
            resp,
            resp.get("metadata") if isinstance(resp.get("metadata"), dict) else {},
            resp.get("video") if isinstance(resp.get("video"), dict) else {},
            resp.get("output") if isinstance(resp.get("output"), dict) else {},
        )
        for src in sources:
            for key in ("fps", "frames_per_second", "frame_rate"):
                cand = src.get(key)
                if cand:
                    try:
                        return float(cand)
                    except (TypeError, ValueError):
                        continue
        return 0.0

    def _handle_completion(self, resp):
        """Transition to downloading; trigger _run_download. Idempotent via compare-and-set."""
        self.ensure_one()
        usage = resp.get("usage") or {}
        urls = resp.get("unsigned_urls") or []
        video_url = urls[0] if urls else None

        cost = float(usage.get("cost") or usage.get("cost_in_usd") or 0.0)
        tokens = int(usage.get("tokens") or 0)
        fps_value = self._extract_fps_from_response(resp)

        if not video_url:
            self._fail("no_video_url", "OpenRouter reported completed but returned no URL.")
            return

        # Compare-and-set: only one path (webhook OR cron) transitions to downloading.
        self.env.cr.execute(
            """UPDATE t2av_attempt
               SET state = 'downloading',
                   tokens_used = %s,
                   cost_usd = %s,
                   video_temporary_url = %s,
                   video_expires_at = %s
               WHERE id = %s
                 AND state IN ('submitting', 'processing')
               RETURNING id""",
            (tokens, cost, video_url, fields.Datetime.now() + timedelta(days=7), self.id),
        )
        if not self.env.cr.fetchone():
            _logger.info(
                "T2AV attempt %s: already past polling, skipping duplicate completion.",
                self.id,
            )
            return

        # Refresh ORM cache so subsequent reads see the new values
        self.invalidate_recordset()
        if fps_value:
            self.write({"fps": fps_value})
        self.message_post(body=_("OpenRouter completed; downloading video (cost $%.4f).") % cost)
        self._push_bus()

        # In RabbitMQ pipeline mode the caller invokes _run_download inline
        # within the same transaction; otherwise defer to postcommit.
        if self.env.context.get("t2av_inline_pipeline"):
            return
        self._defer("_run_download")

    # ------------------------------------------------------------------
    # Pipeline — Phase 4: download + upload to S3
    # ------------------------------------------------------------------
    def _run_download(self):
        """Download the MP4 from OpenRouter, upload to S3, create ir.attachment, mark done."""
        self.ensure_one()
        if self.state != _STATE_DOWNLOADING:
            return
        if not self.video_temporary_url:
            self._fail("no_temporary_url", "Download phase entered without a video URL.")
            return

        settings = self._get_settings()
        if not settings["connector_id"]:
            self._fail("no_s3_connector", "S3 connector not configured.")
            return

        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            self._fail("auth", "OpenRouter API key not configured.")
            return

        # v1.2: snapshot job's category, allocate per-category sequence, build dataset filename.
        category = self.job_id.category
        if not category:
            self._fail("no_category", "Job has no category. Cannot upload to dataset.")
            return

        seq_raw = self.env["ir.sequence"].next_by_code(f"t2av.attempt.{category}")
        if not seq_raw:
            self._fail("no_sequence", f"ir.sequence t2av.attempt.{category} not found.")
            return
        try:
            seq_int = int(seq_raw)
        except (ValueError, TypeError):
            self._fail("bad_sequence", f"Sequence returned non-integer: {seq_raw!r}")
            return

        # Note: with no_gap implementation, this sequence is held under a row lock
        # in ir.sequence until the current transaction commits. If the S3 upload
        # below fails AFTER this point, the number is consumed (no rollback because
        # _fail() writes via a fresh cursor that commits independently). Dataset
        # consumers should filter on video_file IS NOT NULL, not on sequence_number
        # being contiguous.
        video_filename = f"T2AV_{category}_{seq_int:06d}.mp4"
        s3_key = f"T2AV/{category}/{video_filename}"

        try:
            info = s3_publisher.persist_video_to_s3(
                self.env,
                connector_id=settings["connector_id"],
                record_id=self.id,
                record_name=video_filename.rsplit(".mp4", 1)[0],
                mp4_url=self.video_temporary_url,
                prefix="",                # ignored when object_key is provided
                object_key=s3_key,        # NEW: dataset path layout
                auth_bearer=api_key,
                verify=settings["verify_after_upload"],
            )
        except s3_publisher.S3VerificationError as e:
            self._fail("integrity_mismatch", str(e))
            return
        except s3_publisher.S3DownloadError as e:
            self._fail("download_failed", str(e))
            return
        except s3_publisher.S3UploadError as e:
            self._fail("s3_upload_failed", str(e))
            return
        except s3_publisher.S3StorageError as e:
            self._fail("s3_error", str(e))
            return
        except Exception as e:
            _logger.exception("T2AV: unexpected error in _run_download")
            self._fail("persist_error", str(e))
            return

        # Update the attempt with S3 metadata
        self.write({
            "state": _STATE_DONE,
            "completed_at": fields.Datetime.now(),
            "category": category,           # snapshot
            "sequence_number": seq_int,     # allocated
            "video_s3_bucket": info["s3_bucket"],
            "video_s3_key": info["s3_key"],
            "video_s3_url": info["s3_url"],
            "video_s3_etag": info["etag"],
            "video_sha256": info["sha256"],
            "video_size_bytes": info["size"],
        })

        # Eager ir.attachment creation — appears in the job's chatter automatically.
        try:
            with self.env.cr.savepoint():
                attachment = self.env["ir.attachment"].sudo().create({
                    "name": f"{self.display_name or self.job_id.name}.mp4",
                    "res_model": "t2av.generation",
                    "res_id": self.job_id.id,
                    "type": "url",
                    "url": self.video_play_url or info["s3_url"],
                    "mimetype": "video/mp4",
                    "t2av_job_id": self.job_id.id,
                    "t2av_attempt_id": self.id,
                })
                self.write({"video_attachment_id": attachment.id})
        except Exception:
            _logger.exception(
                "T2AV: failed to create ir.attachment for attempt %s "
                "(non-fatal; download still succeeded)",
                self.id,
            )

        self.message_post(body=_(
            "Completed in %ds. %d tokens, $%.4f. URL: %s"
        ) % (self.duration_seconds or 0, self.tokens_used, self.cost_usd, info["s3_url"]))
        self._push_bus()
        self._on_state_done()

    def _on_state_done(self):
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        raw = (icp.get_param("t2av.enable_gemini_qc", "False") or "").strip().lower()
        gemini_qc_active = raw in ("1", "true", "yes", "on")
        if gemini_qc_active:
            return
        if self.review_ids:
            return
        try:
            with self.env.cr.savepoint():
                self.env["t2av.video.review"].sudo().create({
                    "attempt_id": self.id,
                    "provider": "human",
                    "state": "queued",
                })
        except Exception:
            _logger.exception(
                "T2AV: failed to auto-enqueue human review for attempt %s "
                "(non-fatal; download already persisted)",
                self.id,
            )

    # ------------------------------------------------------------------
    # Webhook entrypoint — called from controllers/webhook.py
    # ------------------------------------------------------------------
    def _handle_webhook_event(self, event_type, data):
        """Process a verified OpenRouter webhook event."""
        self.ensure_one()
        status = (data.get("status") or "").lower()
        self.write({
            "openrouter_status": status,
            "last_polled_at": fields.Datetime.now(),
            "raw_response_json": json.dumps(data)[:65536],
        })
        if status in _OR_STATUS_COMPLETED:
            self._handle_completion(data)
        elif status in _OR_STATUS_FAILED or status in _OR_STATUS_EXPIRED:
            self._fail("openrouter_failed", self._extract_or_error(data))
        elif status in _OR_STATUS_CANCELLED:
            self.write({"state": _STATE_CANCELLED})
            self._push_bus()
        else:
            self._push_bus()

    # ------------------------------------------------------------------
    # Bus notification for OWL live-status widget
    # ------------------------------------------------------------------
    def _push_bus(self):
        """Notify the OWL t2av_live_status widget that this attempt's state changed."""
        self.ensure_one()
        try:
            partner = self.job_id.user_id.partner_id
            if not partner:
                return
            self.env["bus.bus"]._sendone(
                _BUS_CHANNEL, _BUS_TYPE, {
                    "id": self.job_id.id,
                    "attempt_id": self.id,
                    "attempt_number": self.attempt_number,
                    "state": self.state,
                    "openrouter_status": self.openrouter_status or "",
                    "poll_attempts": self.poll_attempts,
                    "video_s3_url": self.video_s3_url or "",
                    "error_message": self.error_message or "",
                    "tokens_used": self.tokens_used,
                    "cost_usd": self.cost_usd,
                },
            )
        except Exception:
            _logger.exception("T2AV: failed to push bus.bus update for attempt %s", self.id)

    # ------------------------------------------------------------------
    # Single failure path
    # ------------------------------------------------------------------
    def _fail(self, error_code, error_message):
        """Mark this attempt failed and push bus."""
        self.ensure_one()
        self.write({
            "state": _STATE_FAILED,
            "error_code": error_code,
            "error_message": (error_message or "")[:2000],
            "completed_at": fields.Datetime.now(),
        })
        self.message_post(body=_("Failed (%s): %s") % (error_code, (error_message or "")[:500]))
        self._push_bus()

    # ------------------------------------------------------------------
    # Per-attempt UI actions (used by the Attempts notebook tab on the job)
    # ------------------------------------------------------------------
    def action_open_video(self):
        """Open THIS attempt's video URL in a new tab (presigned if possible)."""
        self.ensure_one()
        if self.state != _STATE_DONE:
            raise UserError(_("This attempt has no completed video to open."))
        url = self.video_play_url or self.video_s3_url
        if not url:
            raise UserError(_("Video URL not available for this attempt."))
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}

    def action_download(self):
        """Download THIS attempt's MP4 via its ir.attachment, if present."""
        self.ensure_one()
        if self.state != _STATE_DONE:
            raise UserError(_("This attempt has no completed video to download."))
        if self.video_attachment_id:
            return {
                "type": "ir.actions.act_url",
                "url": f"/web/content/{self.video_attachment_id.id}?download=true",
                "target": "self",
            }
        return self.action_open_video()

    # ------------------------------------------------------------------
    # Settings helper
    # ------------------------------------------------------------------
    def _get_settings(self):
        """Read commonly-used config_parameters into a dict for the pipeline."""
        icp = self.env["ir.config_parameter"].sudo()
        try:
            connector_id = int(icp.get_param("t2av.s3_connector_id") or 0)
        except (ValueError, TypeError):
            connector_id = 0
        return {
            "connector_id": connector_id,
            "s3_prefix": icp.get_param("t2av.s3_prefix") or "t2av/",
            "verify_after_upload": icp.get_param("t2av.verify_after_upload") in ("True", "true", "1"),
            "poll_interval_seconds": int(icp.get_param("t2av.poll_interval_seconds", "15") or "15"),
            "max_poll_attempts": int(icp.get_param("t2av.max_poll_attempts", "80") or "80"),
            "http_referer": icp.get_param("t2av.http_referer") or "",
            "app_title": icp.get_param("t2av.app_title") or "Ethara T2AV",
        }

    # ------------------------------------------------------------------
    # Cron — poll in-flight attempts + watchdog rescues
    # ------------------------------------------------------------------
    @api.model
    def _cron_poll_openrouter(self):
        """Cron entry point — polls in-flight attempts and rescues stuck jobs.

        Called every minute by ir.cron. Three responsibilities:

        1. Mark attempts stuck in ``submitting`` (>5 min, no job_id) as failed.
           This handles the case where a worker crashed mid-submit.
        2. Mark attempts stuck in ``downloading`` (>30 min) as failed.
           OpenRouter download or S3 upload hung.
        3. Poll active attempts whose ``last_polled_at`` is stale relative to
           ``poll_interval_seconds`` (default 15s).

        Batch-limited to 50 records per tick and 120s wall clock so a slow
        poll never blocks the next cron tick. Each record's
        transaction is independent (savepoint per record) so one failure
        doesn't cascade.
        """
        now = fields.Datetime.now()
        start_ts = time.time()
        settings_icp = self.env["ir.config_parameter"].sudo()
        poll_interval = int(settings_icp.get_param("t2av.poll_interval_seconds", "15") or "15")
        max_poll_attempts = int(settings_icp.get_param("t2av.max_poll_attempts", "80") or "80")

        # 1. Watchdog: stuck submitting (no job_id assigned after 5 min)
        stuck_submitting = self.search([
            ("state", "=", _STATE_SUBMITTING),
            ("submitted_at", "<", fields.Datetime.subtract(now, minutes=5)),
            ("openrouter_job_id", "in", [False, None, ""]),
        ], limit=50)
        for rec in stuck_submitting:
            with self.env.cr.savepoint():
                rec._fail("submit_timeout", "Submission did not return a job id within 5 minutes.")

        # 2. Watchdog: stuck downloading (>30 min)
        stuck_downloading = self.search([
            ("state", "=", _STATE_DOWNLOADING),
            ("last_polled_at", "<", fields.Datetime.subtract(now, minutes=30)),
        ], limit=50)
        for rec in stuck_downloading:
            with self.env.cr.savepoint():
                rec._fail("download_timeout", "Download did not complete within 30 minutes.")

        # 3. Watchdog: max poll attempts exceeded
        poll_exceeded = self.search([
            ("state", "in", (_STATE_SUBMITTING, _STATE_PROCESSING)),
            ("poll_attempts", ">=", max_poll_attempts),
        ], limit=50)
        for rec in poll_exceeded:
            with self.env.cr.savepoint():
                rec._fail(
                    "poll_exceeded",
                    f"Exceeded max poll attempts ({max_poll_attempts}).",
                )

        # 4. Poll active attempts whose last poll is stale
        stale_threshold = fields.Datetime.subtract(now, seconds=poll_interval)
        active = self.search([
            ("state", "in", (_STATE_SUBMITTING, _STATE_PROCESSING)),
            "|", ("last_polled_at", "=", False), ("last_polled_at", "<", stale_threshold),
        ], limit=50, order="last_polled_at asc nulls first")

        polled = 0
        completed = 0
        failed = 0
        for rec in active:
            # Wall-clock cap: 120s. If we're past it, leave the rest for next tick.
            if time.time() - start_ts > 120:
                _logger.warning(
                    "T2AV cron: wall-clock cap hit after %d records; "
                    "leaving %d more for next tick",
                    polled, len(active) - polled,
                )
                break
            with self.env.cr.savepoint():
                try:
                    rec._run_poll()
                    polled += 1
                    if rec.state == _STATE_DOWNLOADING:
                        completed += 1
                    elif rec.state == _STATE_FAILED:
                        failed += 1
                except Exception:
                    _logger.exception(
                        "T2AV cron: _run_poll raised on attempt %s", rec.id,
                    )
                    failed += 1

        if active or stuck_submitting or stuck_downloading or poll_exceeded:
            _logger.info(
                "T2AV cron: polled=%d, completed=%d, failed=%d, "
                "stuck-submitting-rescued=%d, stuck-downloading-rescued=%d, "
                "poll-exceeded-rescued=%d, elapsed=%.1fs",
                polled, completed, failed,
                len(stuck_submitting), len(stuck_downloading), len(poll_exceeded),
                time.time() - start_ts,
            )
