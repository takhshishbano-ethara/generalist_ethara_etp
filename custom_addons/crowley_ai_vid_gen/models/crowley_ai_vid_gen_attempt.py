# -*- coding: utf-8 -*-
"""Single generation attempt within a video job.

A *crowley.ai.vid.gen.job* now owns up to **three** attempts:
``attempt #1`` (the original generation) plus up to two **refinements**
where the user revised the prompt to fix something they didn't like in
the previous result. Every attempt is a full Seedance job in its own
right — fresh OpenRouter submission, fresh S3 object — but the seed
stays constant across attempts so the only delta between runs is the
prompt itself.

All per-run state — OpenRouter ids, polling URL, S3 key, SHA-256,
cost, timestamps, error_message — lives on the attempt row. The job
record proxies these through its ``active_attempt_id`` for backwards
compatibility with views and the studio.
"""

import logging
import traceback
import uuid
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_TERMINAL_STATES = ("ready", "failed", "cancelled")


class CrowleyAiVidGenAttempt(models.Model):
    _name = "crowley.ai.vid.gen.attempt"
    _description = "Crowley AI Video Generation Attempt"
    _order = "job_id, attempt_number"

    _openrouter_job_id_unique = models.Constraint(
        "unique(openrouter_job_id)",
        "OpenRouter job id must be unique across all attempts.",
    )
    _job_attempt_number_unique = models.Constraint(
        "unique(job_id, attempt_number)",
        "Each job can hold at most one attempt per attempt number.",
    )

    job_id = fields.Many2one(
        "crowley.ai.vid.gen.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    attempt_number = fields.Integer(
        required=True,
        readonly=True,
        copy=False,
        help="1 for the original generation; 2 or 3 for user-driven refinements.",
    )
    display_name = fields.Char(
        compute="_compute_display_name",
        store=False,
    )

    prompt = fields.Text(
        required=True,
        readonly=True,
        help="The user's prompt for this specific attempt — may differ from prior attempts.",
    )
    effective_prompt = fields.Text(
        readonly=True,
        help="Exact prompt string sent to OpenRouter (system prefix + user prompt). "
             "Persisted for audit so the prompt log requirement is satisfied.",
    )

    seed = fields.Integer(readonly=True)

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
        default="draft",
        required=True,
        index=True,
        readonly=True,
        copy=False,
    )

    openrouter_job_id = fields.Char(readonly=True, copy=False, index=True)
    openrouter_polling_url = fields.Char(readonly=True, copy=False)
    poll_attempts = fields.Integer(default=0, readonly=True, copy=False)
    last_poll_at = fields.Datetime(readonly=True, copy=False)
    webhook_idempotency_key = fields.Char(readonly=True, copy=False, index=True)

    s3_bucket = fields.Char(readonly=True, copy=False)
    s3_key = fields.Char(readonly=True, copy=False, index=True)
    s3_etag = fields.Char(readonly=True, copy=False)
    source_sha256 = fields.Char(readonly=True, copy=False)
    file_size = fields.Integer(readonly=True, copy=False)
    mimetype = fields.Char(default="video/mp4", readonly=True, copy=False)

    cost_usd = fields.Float(readonly=True, copy=False, digits=(10, 4))
    error_message = fields.Text(readonly=True, copy=False)

    submitted_at = fields.Datetime(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)

    video_play_url = fields.Char(
        compute="_compute_video_play_url",
        store=False,
        readonly=True,
    )

    @api.depends("s3_key", "state", "mimetype", "job_id.name", "attempt_number")
    def _compute_video_play_url(self):
        storage = self.env["crowley.ai.vid.gen.s3.storage"]
        ICP = self.env["ir.config_parameter"].sudo()
        ttl = int(ICP.get_param("crowley_ai_vid_gen.presigned_ttl_seconds", "300"))
        for rec in self:
            if rec.state != "ready" or not rec.s3_key:
                rec.video_play_url = ""
                continue
            try:
                rec.video_play_url = storage.presigned_get_url(
                    rec.s3_key,
                    expires_in=ttl,
                    mimetype=rec.mimetype or "video/mp4",
                    disposition="inline",
                    filename=f"{rec.job_id.name or 'video'}-a{rec.attempt_number}.mp4",
                )
            except Exception:
                _logger.exception(
                    "Failed to generate presigned URL for attempt %s",
                    rec.id,
                )
                rec.video_play_url = ""

    @api.depends("job_id.name", "attempt_number")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = (
                f"{rec.job_id.name or 'CVG/?'} · attempt {rec.attempt_number}"
            )

    def _defer(self, callback_name, *args):
        """Schedule a method to run after-commit on a fresh cursor.

        Dual-cursor failure recording: if the deferred call raises, the
        original cursor is rolled back and we open a SECOND cursor to
        persist the failure state — otherwise the failure write would
        also be rolled back and the attempt would be invisibly stuck.
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
                    attempt = env["crowley.ai.vid.gen.attempt"].browse(rec_id).exists()
                    if attempt:
                        getattr(attempt, callback_name)(*args)
            except Exception:  # noqa: BLE001 — surface failure on the attempt row
                _logger.exception(
                    "Deferred Crowley attempt task failed (attempt %s, cb %s)",
                    rec_id,
                    callback_name,
                )
                try:
                    registry = odoo.modules.registry.Registry(db_name)
                    with registry.cursor() as cr2:
                        env2 = odoo.api.Environment(cr2, uid, {})
                        attempt = env2["crowley.ai.vid.gen.attempt"].browse(rec_id).exists()
                        if attempt:
                            attempt.write(
                                {
                                    "state": "failed",
                                    "error_message": (traceback.format_exc() or "")[:8000],
                                }
                            )
                except Exception:  # noqa: BLE001
                    _logger.exception("Failed to mark attempt %s as failed", rec_id)

        self.env.cr.postcommit.add(_run)

    def _run_submit(self):
        """Submit this attempt to OpenRouter and transition to ``polling``."""
        self.ensure_one()
        self.write({"state": "submitting"})

        ICP = self.env["ir.config_parameter"].sudo()
        callback_url = self.job_id._build_callback_url(ICP)
        effective_prompt = self.job_id._build_effective_prompt_for(
            ICP, self.prompt
        )

        self.write({"effective_prompt": effective_prompt})

        client = self.env["crowley.ai.vid.gen.openrouter.client"]
        resp = client.submit_video_job(
            model=self.job_id.model,
            prompt=effective_prompt,
            duration=self.job_id.duration,
            resolution=self.job_id.resolution,
            aspect_ratio=self.job_id.aspect_ratio,
            generate_audio=self.job_id.generate_audio,
            seed=(self.seed or None),
            callback_url=callback_url,
        )

        self.write(
            {
                "openrouter_job_id": resp["id"],
                "openrouter_polling_url": resp["polling_url"],
                "state": "polling",
            }
        )
        self.job_id.message_post(
            body=_(
                "Attempt %(n)s submitted to OpenRouter (job id %(jid)s)."
            ) % {"n": self.attempt_number, "jid": resp["id"]},
        )

    def _run_poll(self):
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        max_attempts = int(ICP.get_param("crowley_ai_vid_gen.max_poll_attempts", "60"))
        if self.poll_attempts >= max_attempts:
            self.write(
                {
                    "state": "failed",
                    "error_message": _(
                        "Timed out waiting for OpenRouter completion "
                        "after %s polls."
                    ) % max_attempts,
                }
            )
            return
        client = self.env["crowley.ai.vid.gen.openrouter.client"]
        payload = client.poll_job(self.openrouter_polling_url)
        self.write(
            {
                "poll_attempts": self.poll_attempts + 1,
                "last_poll_at": fields.Datetime.now(),
            }
        )
        status = (payload or {}).get("status")
        if status in ("pending", "in_progress"):
            return
        if status == "completed":
            self._handle_completion(payload)
            return
        if status in ("failed", "cancelled", "expired"):
            err = (payload or {}).get("error") or status
            self.write(
                {
                    "state": "cancelled" if status == "cancelled" else "failed",
                    "error_message": _("OpenRouter reported %s: %s")
                    % (status, str(err)[:1000]),
                }
            )
            return
        self.write(
            {
                "state": "failed",
                "error_message": _("Unknown OpenRouter status: %s") % status,
            }
        )

    def _handle_webhook_event(self, event_type, data):
        """Webhook entrypoint — HMAC-validated upstream in the controller."""
        self.ensure_one()
        if self.state in _TERMINAL_STATES:
            _logger.info(
                "Attempt %s already terminal (%s); ignoring webhook %s.",
                self.id, self.state, event_type,
            )
            return
        if event_type == "video.generation.completed":
            self._handle_completion(data)
            return
        if event_type == "video.generation.failed":
            err = (data or {}).get("error") or "OpenRouter reported failure"
            self.write(
                {"state": "failed", "error_message": _("OpenRouter: %s") % str(err)[:1000]}
            )
            return
        if event_type == "video.generation.cancelled":
            self.write({"state": "cancelled", "error_message": _("Cancelled at OpenRouter.")})
            return
        if event_type == "video.generation.expired":
            self.write(
                {"state": "failed", "error_message": _("OpenRouter content URL expired.")}
            )
            return
        _logger.warning("Crowley webhook: unknown event_type %s", event_type)

    def _handle_completion(self, payload):
        """Atomic compare-and-set guards against webhook + cron racing
        the same completion — see Oracle review C4 from the prior pass.
        """
        self.ensure_one()
        urls = (payload or {}).get("unsigned_urls") or []
        if not urls:
            self.write(
                {
                    "state": "failed",
                    "error_message": _(
                        "OpenRouter completion payload has no video URL."
                    ),
                }
            )
            return
        content_url = urls[0]
        cost = ((payload or {}).get("usage") or {}).get("cost") or 0.0

        self.env.cr.execute(
            "UPDATE crowley_ai_vid_gen_attempt "
            "SET state='downloading', cost_usd=%s "
            "WHERE id = %s AND state IN ('submitting','polling') "
            "RETURNING id",
            (cost, self.id),
        )
        if not self.env.cr.fetchone():
            _logger.info(
                "Attempt %s already past polling; skipping duplicate completion.",
                self.id,
            )
            return
        self.invalidate_recordset(["state", "cost_usd"])
        self._defer("_run_download", content_url)

    def _run_download(self, content_url):
        """Stream the MP4 from OpenRouter into S3 and mark the attempt ready.

        All attempts of a single job share an S3 folder
        (``<prefix>/<YYYY>/<MM>/<job_id>/<uuid>.mp4``) per the product
        spec — the per-attempt UUID keeps the filename unique while the
        parent folder collects every variant of the same job.
        """
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()

        missing = [
            label for label, key in (
                ("S3 bucket", "crowley_ai_vid_gen.s3_bucket"),
                ("AWS access key", "crowley_ai_vid_gen.s3_access_key"),
                ("AWS secret key", "crowley_ai_vid_gen.s3_secret_key"),
            )
            if not (ICP.get_param(key) or "").strip()
        ]
        endpoint = (ICP.get_param("crowley_ai_vid_gen.s3_endpoint_url") or "").strip()
        if missing and not endpoint:
            raise UserError(_(
                "Cannot upload the generated video — S3 is not configured.\n"
                "Missing: %s.\n"
                "Open Settings → Crowley AI Vid Gen and re-enter the value(s)."
            ) % ", ".join(missing))

        prefix = ICP.get_param("crowley_ai_vid_gen.s3_key_prefix", "crowley-seedance/")
        prefix = (prefix or "").lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

        key = (
            f"{prefix}"
            f"{datetime.utcnow().strftime('%Y/%m')}/"
            f"{self.job_id.id}/{uuid.uuid4().hex}.mp4"
        )

        storage = self.env["crowley.ai.vid.gen.s3.storage"]
        bucket = storage._bucket()

        client = self.env["crowley.ai.vid.gen.openrouter.client"]
        result = client.stream_to_s3(
            content_url,
            bucket=bucket,
            key=key,
            mimetype="video/mp4",
        )

        verify_after = (
            (ICP.get_param("crowley_ai_vid_gen.verify_after_upload", "False") or "")
            .strip()
            .lower()
            in ("1", "true", "yes")
        )
        if verify_after:
            stored_hash = storage.verify_object_sha256(bucket=bucket, key=key)
            if stored_hash != result["sha256"]:
                raise UserError(_(
                    "Post-upload verification failed: source SHA-256 (%s) "
                    "does not match stored SHA-256 (%s). Object: s3://%s/%s"
                ) % (result["sha256"], stored_hash, bucket, key))

        self.write(
            {
                "s3_bucket": bucket,
                "s3_key": key,
                "s3_etag": result["etag"],
                "file_size": result["size"],
                "source_sha256": result["sha256"],
                "state": "ready",
                "completed_at": fields.Datetime.now(),
            }
        )
        self.job_id.invalidate_recordset(["active_attempt_id", "state", "video_play_url"])
        self.job_id.message_post(
            body=_(
                "Attempt %(n)s ready — %(size)s bytes, SHA-256 %(sha)s, cost $%(cost).4f."
            ) % {
                "n": self.attempt_number,
                "size": result["size"],
                "sha": result["sha256"],
                "cost": self.cost_usd or 0.0,
            }
        )

    def action_resume_download(self):
        """Re-fetch this attempt's already-generated video without re-submitting.

        Used when OpenRouter produced the video but the local download
        failed (typical cause: S3 credentials missing). Skips the
        submit + poll cycle and goes straight to the download step,
        saving the cost of a duplicate generation. The OpenRouter
        content URL is valid for 24 hours after generation — if expired
        the user must use ``action_retry`` on the job (full re-spawn).
        """
        self.ensure_one()
        if self.state != "failed":
            raise UserError(_("Resume is only available on failed attempts."))
        if not self.openrouter_polling_url:
            raise UserError(_("Cannot resume — this attempt never reached OpenRouter."))
        client = self.env["crowley.ai.vid.gen.openrouter.client"]
        payload = client.poll_job(self.openrouter_polling_url)
        status = (payload or {}).get("status")
        if status != "completed":
            raise UserError(_(
                "OpenRouter job is in state '%s' — cannot resume the download. "
                "Use Retry on the job to submit a fresh attempt (incurs a new cost)."
            ) % status)
        urls = (payload or {}).get("unsigned_urls") or []
        if not urls:
            raise UserError(_("OpenRouter response has no video URL — cannot resume."))
        self.env.cr.execute(
            "UPDATE crowley_ai_vid_gen_attempt SET state='downloading', error_message=NULL "
            "WHERE id=%s AND state='failed' RETURNING id",
            (self.id,),
        )
        if not self.env.cr.fetchone():
            raise UserError(_("Attempt is no longer in a resumable state."))
        self.invalidate_recordset(["state", "error_message"])
        self.job_id.message_post(
            body=_("Resuming download for attempt %s (skipping re-submission).")
            % self.attempt_number,
        )
        self._defer("_run_download", urls[0])
        return True

    @api.model
    def _cron_poll_openrouter(self):
        """Fallback poller — runs every minute via ``ir.cron``.

        Rescues attempts wedged in ``submitting`` (>5min, worker crashed)
        and ``downloading`` (>30min, S3 stream never finished); then
        polls every ``state='polling'`` attempt whose ``last_poll_at``
        is older than the configured interval. Capped at 50 records /
        120s wall-clock per tick.
        """
        import time as _time

        ICP = self.env["ir.config_parameter"].sudo()
        interval = int(ICP.get_param("crowley_ai_vid_gen.poll_interval_seconds", "60"))
        threshold = fields.Datetime.now() - timedelta(seconds=interval)

        stuck_submit = self.search(
            [
                ("state", "=", "submitting"),
                ("submitted_at", "<=", fields.Datetime.now() - timedelta(minutes=5)),
            ]
        )
        if stuck_submit:
            stuck_submit.write(
                {
                    "state": "failed",
                    "error_message": _(
                        "Submission worker did not return — marking failed. Use Retry."
                    ),
                }
            )
            self.env.cr.commit()

        stuck_dl = self.search(
            [
                ("state", "=", "downloading"),
                ("submitted_at", "<=", fields.Datetime.now() - timedelta(minutes=30)),
            ]
        )
        if stuck_dl:
            stuck_dl.write(
                {
                    "state": "failed",
                    "error_message": _(
                        "Download worker did not return — marking failed. Use Retry."
                    ),
                }
            )
            self.env.cr.commit()

        candidates = self.search(
            [
                ("state", "=", "polling"),
                "|",
                ("last_poll_at", "=", False),
                ("last_poll_at", "<=", threshold),
            ],
            limit=50,
        )
        start = _time.monotonic()
        for attempt in candidates:
            if _time.monotonic() - start > 120:
                _logger.info(
                    "Cron poller hit 120s wall-clock cap; remaining attempts deferred."
                )
                break
            try:
                attempt._run_poll()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001 — isolate failures across the batch
                _logger.exception("Cron poll failed for attempt %s", attempt.id)
                self.env.cr.rollback()
