from __future__ import annotations

import json
import logging
import traceback

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import credential_manager
from ..services import review_client

_logger = logging.getLogger(__name__)

_STATE_QUEUED = "queued"
_STATE_SUBMITTING = "submitting"
_STATE_DONE = "done"
_STATE_ERROR = "error"

_NON_TERMINAL = {_STATE_QUEUED, _STATE_SUBMITTING}
_TERMINAL = {_STATE_DONE, _STATE_ERROR}

_ALLOWED_TRANSITIONS = {
    _STATE_QUEUED:     {_STATE_SUBMITTING, _STATE_ERROR},
    _STATE_SUBMITTING: {_STATE_DONE, _STATE_ERROR},
    _STATE_DONE:       set(),
    _STATE_ERROR:      set(),
}


def _mask_model_id(value):
    if not value:
        return value
    if not value.startswith("arn:aws:"):
        return value
    parts = value.split(":", 5)
    if len(parts) >= 6:
        parts[4] = "***"
        return ":".join(parts)
    return value


class CrowleyVideoReview(models.Model):
    _name = "crowley.video.review"
    _description = "Crowley Video QC Review"
    _order = "attempt_id, create_date desc"
    _rec_name = "display_name"

    attempt_id = fields.Many2one(
        "crowley.attempt", string="Attempt",
        required=True, ondelete="cascade", index=True,
    )
    job_id = fields.Many2one(
        "crowley.generation", related="attempt_id.job_id",
        string="Job", store=True, index=True,
    )
    display_name = fields.Char(compute="_compute_display_name", store=False)

    model_id = fields.Char(
        string="Model ID", readonly=True, copy=False,
        groups="base.group_no_one",
    )
    model_id_display = fields.Char(
        string="Model", compute="_compute_model_id_display",
        help="Model identifier with AWS account ID masked for safe UI display.",
    )
    region = fields.Char(string="AWS Region", readonly=True, copy=False)
    provider = fields.Selection(
        [("bedrock", "AWS Bedrock"), ("openrouter", "OpenRouter")],
        string="Provider", readonly=True, copy=False, index=True,
        help="Which LLM gateway processed this review.",
    )
    bedrock_request_id = fields.Char(
        string="Provider Request ID", readonly=True, copy=False,
        help="Bedrock x-amzn-RequestId or OpenRouter generation id — use when filing a support ticket.",
    )

    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("submitting", "Submitting"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Status", default=_STATE_QUEUED, readonly=True,
        copy=False, index=True,
    )

    verdict = fields.Selection(
        [
            ("accept", "ACCEPT"),
            ("review", "REVIEW"),
            ("reject", "REJECT"),
        ],
        string="Verdict", readonly=True, copy=False, index=True,
    )
    passed = fields.Boolean(
        string="Passed", compute="_compute_passed", store=True,
    )

    fatal_count = fields.Integer(readonly=True, copy=False)
    major_count = fields.Integer(readonly=True, copy=False)
    minor_count = fields.Integer(readonly=True, copy=False)
    unverifiable_count = fields.Integer(readonly=True, copy=False)
    num_frames = fields.Integer(string="Frames Sampled", readonly=True, copy=False)

    regenerate_recommended = fields.Boolean(readonly=True, copy=False)
    rebuilder_hint = fields.Char(readonly=True, copy=False)

    prose_report = fields.Text(string="Prose Report", readonly=True, copy=False)
    findings_json = fields.Text(string="Findings (JSON)", readonly=True, copy=False)
    rendered_info = fields.Text(string="Rendered Info (JSON)", readonly=True, copy=False)

    submitted_at = fields.Datetime(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    input_tokens = fields.Integer(readonly=True, copy=False)
    output_tokens = fields.Integer(readonly=True, copy=False)
    tokens_used = fields.Integer(
        compute="_compute_tokens_used", store=True,
    )

    error_message = fields.Text(readonly=True, copy=False)
    error_code = fields.Char(readonly=True, copy=False)

    cost_usd = fields.Float(
        string="Cost (USD)", digits=(12, 6), readonly=True, copy=False,
        compute="_compute_cost_usd", store=True,
        help="Bedrock Claude 3.5 Sonnet vision pricing: $3/M input tokens, $15/M output tokens.",
    )

    @api.depends("input_tokens", "output_tokens")
    def _compute_cost_usd(self):
        for rec in self:
            rec.cost_usd = (
                (rec.input_tokens or 0) * 3.0 / 1_000_000.0
                + (rec.output_tokens or 0) * 15.0 / 1_000_000.0
            )

    @api.model
    def _cron_watchdog(self):
        threshold = fields.Datetime.subtract(fields.Datetime.now(), minutes=15)
        stuck = self.search([
            ("state", "in", (_STATE_QUEUED, _STATE_SUBMITTING)),
            ("submitted_at", "<", threshold),
        ], limit=100)
        for rec in stuck:
            with self.env.cr.savepoint():
                try:
                    rec._fail(
                        "watchdog_timeout",
                        f"Stuck in '{rec.state}' for >15 minutes; reset by watchdog cron.",
                    )
                except Exception:
                    _logger.exception("Crowley video-review watchdog: failed on id=%s", rec.id)
        if stuck:
            _logger.info("Crowley video-review watchdog: reset %d stuck row(s)", len(stuck))

    @api.depends("attempt_id.display_name", "create_date")
    def _compute_display_name(self):
        for rec in self:
            base = rec.attempt_id.display_name or "(no attempt)"
            rec.display_name = f"{base} review"

    @api.depends("model_id")
    def _compute_model_id_display(self):
        for rec in self:
            rec.model_id_display = _mask_model_id(rec.model_id)

    @api.depends("verdict")
    def _compute_passed(self):
        for rec in self:
            rec.passed = rec.verdict == "accept"

    @api.depends("input_tokens", "output_tokens")
    def _compute_tokens_used(self):
        for rec in self:
            rec.tokens_used = (rec.input_tokens or 0) + (rec.output_tokens or 0)

    def write(self, vals):
        if "state" in vals:
            new_state = vals["state"]
            for rec in self:
                old = rec.state
                if old == new_state:
                    continue
                allowed = _ALLOWED_TRANSITIONS.get(old, set())
                if new_state not in allowed:
                    raise ValidationError(_(
                        "Cannot transition review %(name)s from %(old)s to %(new)s."
                    ) % {"name": rec.display_name, "old": old, "new": new_state})
        return super().write(vals)

    def _defer(self, callback_name, *args):
        import odoo
        db_name = self.env.cr.dbname
        rec_id = self.id
        uid = self.env.uid

        def _run():
            try:
                registry = odoo.modules.registry.Registry(db_name)
                with registry.cursor() as cr:
                    env = odoo.api.Environment(cr, uid, {})
                    rec = env["crowley.video.review"].browse(rec_id).exists()
                    if rec:
                        getattr(rec, callback_name)(*args)
            except Exception:
                _logger.exception(
                    "Crowley video-review deferred task failed (id=%s, cb=%s)",
                    rec_id, callback_name,
                )
                try:
                    registry = odoo.modules.registry.Registry(db_name)
                    with registry.cursor() as cr2:
                        env2 = odoo.api.Environment(cr2, uid, {})
                        rec = env2["crowley.video.review"].browse(rec_id).exists()
                        if rec and rec.state in _NON_TERMINAL:
                            rec.write({
                                "state": _STATE_ERROR,
                                "error_code": "internal_error",
                                "error_message": (traceback.format_exc() or "")[:8000],
                                "completed_at": fields.Datetime.now(),
                            })
                except Exception:
                    _logger.exception(
                        "Crowley: failed to mark video-review %s as error", rec_id,
                    )

        self.env.cr.postcommit.add(_run)

    def _run_review(self):
        self.ensure_one()
        if self.state != _STATE_QUEUED:
            return
        try:
            self._run_review_body()
        except Exception as e:
            _logger.exception("Crowley video-review: unexpected error in _run_review")
            if self.state in _NON_TERMINAL:
                self._fail("internal_error", str(e)[:500])

    def _run_review_body(self):
        attempt = self.attempt_id
        if not attempt.exists():
            self._fail("no_attempt", "Parent attempt no longer exists.")
            return
        if attempt.state != "done":
            self._fail(
                "attempt_not_done",
                f"Attempt is in state {attempt.state!r}, expected 'done'.",
            )
            return

        video_url = attempt.video_play_url or attempt.video_s3_url
        if not video_url:
            self._fail("no_video_url", "Attempt has no playable video URL.")
            return

        icp = self.env["ir.config_parameter"].sudo()
        provider = (icp.get_param(
            "crowley.review_provider", review_client.DEFAULT_PROVIDER,
        ) or review_client.DEFAULT_PROVIDER).strip().lower()

        access_key = ""
        secret_key = ""
        openrouter_api_key = ""
        if provider == "bedrock":
            access_key = credential_manager.get_aws_access_key(self.env)
            secret_key = credential_manager.get_aws_secret_key(self.env)
            if not access_key or not secret_key:
                self._fail(
                    "aws_creds_missing",
                    "AWS Access Key / Secret Key not configured for Bedrock review. "
                    "Set them in Settings > Crowley.",
                )
                return
            model_id = icp.get_param(
                "crowley.bedrock_model_id",
                review_client.DEFAULT_BEDROCK_MODEL_ID,
            )
        elif provider == "openrouter":
            openrouter_api_key = credential_manager.get_openrouter_api_key(self.env)
            if not openrouter_api_key:
                self._fail(
                    "openrouter_creds_missing",
                    "OpenRouter API key not configured for OpenRouter review. "
                    "Set it in Settings > Crowley.",
                )
                return
            model_id = icp.get_param(
                "crowley.openrouter_review_model_id",
                review_client.DEFAULT_OPENROUTER_MODEL_ID,
            )
        else:
            self._fail(
                "bad_provider",
                f"Unknown review provider {provider!r}. Expected 'bedrock' or 'openrouter'.",
            )
            return

        region = icp.get_param(
            "crowley.bedrock_region", review_client.DEFAULT_REGION,
        )
        try:
            num_frames = int(icp.get_param("crowley.review_num_frames", "20"))
        except (TypeError, ValueError):
            num_frames = 20

        job = attempt.job_id
        enriched = (
            job.golden_prompt or job.enriched_prompt or job.prompt or ""
        ).strip()
        if not enriched:
            self._fail(
                "no_prompt",
                "Job has no Golden / Enriched / raw prompt to review against.",
            )
            return
        category = job.category or ""
        style = (job.style or "precise").title()
        priority = (job.priority or "medium").title()
        try:
            duration_seconds = float(job.duration or 5)
        except (TypeError, ValueError):
            duration_seconds = 5.0

        if job.resolution == "1080p":
            resolution = "1920x1080"
        elif job.resolution == "720p":
            resolution = "1280x720"
        elif job.resolution == "480p":
            resolution = "854x480"
        else:
            resolution = "1920x1080"

        self.write({
            "state": _STATE_SUBMITTING,
            "submitted_at": fields.Datetime.now(),
            "model_id": model_id,
            "region": region if provider == "bedrock" else "",
            "provider": provider,
        })

        try:
            result = review_client.review(
                provider=provider,
                access_key=access_key,
                secret_key=secret_key,
                openrouter_api_key=openrouter_api_key,
                region=region,
                model_id=model_id,
                video_url=video_url,
                enriched_prompt=enriched,
                category=category,
                style=style,
                priority=priority,
                duration_seconds=duration_seconds,
                resolution=resolution,
                num_frames=num_frames,
            )
        except review_client.ReviewAuthError as e:
            self._fail("aws_auth", str(e))
            return
        except review_client.ReviewConfigError as e:
            self._fail("config", str(e))
            return
        except review_client.ReviewParseError as e:
            self._fail("parse_error", str(e))
            return
        except review_client.ReviewError as e:
            self._fail("review_error", str(e))
            return
        except Exception as e:
            _logger.exception("Crowley video-review: unexpected error")
            self._fail("internal_error", str(e))
            return

        verdict_norm = (result.get("verdict") or "").lower()
        if verdict_norm not in ("accept", "review", "reject"):
            self._fail("bad_verdict", f"Reviewer returned unknown verdict: {verdict_norm!r}")
            return

        self.write({
            "state": _STATE_DONE,
            "verdict": verdict_norm,
            "prose_report": result.get("prose") or "",
            "findings_json": result.get("raw_json") or "",
            "rendered_info": result.get("rendered_info") or "",
            "fatal_count": result.get("fatal_count") or 0,
            "major_count": result.get("major_count") or 0,
            "minor_count": result.get("minor_count") or 0,
            "unverifiable_count": result.get("unverifiable_count") or 0,
            "regenerate_recommended": bool(result.get("regenerate_recommended")),
            "rebuilder_hint": result.get("rebuilder_hint") or "",
            "input_tokens": result.get("input_tokens") or 0,
            "output_tokens": result.get("output_tokens") or 0,
            "bedrock_request_id": result.get("request_id") or "",
            "num_frames": result.get("num_frames") or 0,
            "completed_at": fields.Datetime.now(),
        })

        job.message_post(body=_(
            "Video review complete: %(verdict)s. "
            "FATAL=%(fatal)d MAJOR=%(major)d MINOR=%(minor)d UNVERIFIABLE=%(unv)d. "
            "Regenerate recommended: %(regen)s%(hint)s"
        ) % {
            "verdict": verdict_norm.upper(),
            "fatal": result.get("fatal_count") or 0,
            "major": result.get("major_count") or 0,
            "minor": result.get("minor_count") or 0,
            "unv": result.get("unverifiable_count") or 0,
            "regen": "yes" if result.get("regenerate_recommended") else "no",
            "hint": (
                f" (hint: {result.get('rebuilder_hint')})"
                if result.get("rebuilder_hint") else ""
            ),
        })

    def _fail(self, error_code, error_message):
        self.ensure_one()
        if self.state in _TERMINAL:
            return
        self.write({
            "state": _STATE_ERROR,
            "error_code": error_code,
            "error_message": (error_message or "")[:4000],
            "completed_at": fields.Datetime.now(),
        })
        job = self.attempt_id.job_id
        if job:
            job.message_post(body=_(
                "Video review failed (%(code)s): %(msg)s"
            ) % {"code": error_code, "msg": (error_message or "")[:200]})
