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

_DEFAULT_REVIEW_MAX_ATTEMPTS = 3
_DEFAULT_REVIEW_MAX_PARALLEL = 4
_DEFAULT_REVIEW_BATCH_SIZE = 8
_DEFAULT_REVIEW_VIDEO_TTL_SECONDS = 900
_PREVIOUS_FAILURE_LIMIT = 5
_PREVIOUS_FAILURE_SEVERITIES = ("FATAL", "MAJOR")


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
        string="Model Verdict", readonly=True, copy=False, index=True,
        help="Raw verdict from the LLM reviewer (Gemini / Bedrock). "
             "Downstream code reads 'Effective Verdict' instead, which "
             "respects human override.",
    )
    human_verdict = fields.Selection(
        [
            ("accept", "ACCEPT"),
            ("review", "REVIEW"),
            ("reject", "REJECT"),
        ],
        string="Human Verdict", readonly=True, copy=False, index=True,
        help="Manager-set override. When non-empty, replaces 'Model Verdict' "
             "in all downstream gates (review_status, sequence sheets).",
    )
    human_override_user_id = fields.Many2one(
        "res.users", string="Override By", readonly=True, copy=False,
        help="Manager who last set or cleared the human verdict.",
    )
    human_override_at = fields.Datetime(
        string="Override At", readonly=True, copy=False,
    )
    human_override_reason = fields.Text(
        string="Override Reason", readonly=True, copy=False,
        help="Justification recorded when the human verdict was set or cleared. "
             "Required for audit; never silently overwritten.",
    )
    effective_verdict = fields.Selection(
        [
            ("accept", "ACCEPT"),
            ("review", "REVIEW"),
            ("reject", "REJECT"),
        ],
        string="Effective Verdict",
        compute="_compute_effective_verdict", store=True, index=True,
        help="human_verdict when set, else verdict. This is the field "
             "downstream code (review_status, sheets, auto-retry) reads.",
    )
    passed = fields.Boolean(
        string="Passed", compute="_compute_passed", store=True,
    )

    reasoning_text = fields.Text(
        string="Reasoning", readonly=True, copy=False,
        help="Reviewer's chain-of-thought captured from the LLM (when supported). "
             "OpenRouter Gemini returns this via the 'reasoning' field; "
             "empty for providers that do not surface reasoning.",
    )
    video_delivery = fields.Selection(
        [("url", "Presigned URL"), ("base64", "Base64 inline")],
        string="Video Delivery", readonly=True, copy=False,
        help="How the video was sent to the reviewer. 'base64' indicates "
             "the URL fetch failed and we fell back to inline upload.",
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

    @api.depends("verdict", "human_verdict")
    def _compute_effective_verdict(self):
        for rec in self:
            rec.effective_verdict = rec.human_verdict or rec.verdict or False

    @api.depends("effective_verdict")
    def _compute_passed(self):
        for rec in self:
            rec.passed = rec.effective_verdict == "accept"

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
        # Atomic claim — only one worker may transition queued -> submitting.
        # Prevents psycopg2.SerializationFailure under REPEATABLE READ when
        # both the postcommit from a Run Review click and the cron dispatcher
        # pick the same row before either commits.
        self.env.cr.execute(
            """UPDATE crowley_video_review
               SET state = 'submitting', submitted_at = %s
               WHERE id = %s AND state = %s
               RETURNING id""",
            (fields.Datetime.now(), self.id, _STATE_QUEUED),
        )
        if not self.env.cr.fetchone():
            _logger.info(
                "Crowley video-review %s: already claimed by another worker; skipping",
                self.id,
            )
            return
        self.invalidate_recordset()
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

        video_url = self._build_review_video_url(attempt)
        if not video_url:
            self._fail("no_video_url", "Attempt has no playable video URL.")
            return

        previous_failures = self._collect_previous_failures(attempt)

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
                previous_failures=previous_failures,
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

        video_delivery = result.get("video_delivery")
        if video_delivery not in ("url", "base64"):
            video_delivery = False

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
            "reasoning_text": result.get("reasoning_text") or "",
            "video_delivery": video_delivery,
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

        self._maybe_create_sheet_row(attempt, verdict_norm)

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

    def _build_review_video_url(self, attempt):
        icp = self.env["ir.config_parameter"].sudo()
        try:
            ttl = int(icp.get_param(
                "crowley.review_video_url_ttl_seconds",
                str(_DEFAULT_REVIEW_VIDEO_TTL_SECONDS),
            ) or _DEFAULT_REVIEW_VIDEO_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = _DEFAULT_REVIEW_VIDEO_TTL_SECONDS
        try:
            connector_id = int(icp.get_param("crowley.s3_connector_id") or 0)
        except (TypeError, ValueError):
            connector_id = 0
        if attempt.video_s3_key and connector_id:
            try:
                return self.env["crowley.s3.storage"].presigned_get_url(
                    connector_id,
                    attempt.video_s3_key,
                    expires_in=ttl,
                    mimetype=attempt.mimetype or "video/mp4",
                    disposition="inline",
                    filename=(
                        f"{attempt.display_name}.mp4" if attempt.display_name else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "Crowley video-review: failed to presign URL for attempt %s",
                    attempt.id,
                )
        return attempt.video_play_url or attempt.video_s3_url or ""

    def _collect_previous_failures(self, attempt, limit=_PREVIOUS_FAILURE_LIMIT):
        rejected = attempt.review_ids.filtered(
            lambda r: r.state == _STATE_DONE
            and r.verdict == "reject"
            and r.id != self.id
        ).sorted("create_date", reverse=True)
        if not rejected:
            return []
        prior = rejected[0]
        if not prior.findings_json:
            return []
        try:
            parsed = json.loads(prior.findings_json)
        except (TypeError, ValueError):
            return []
        findings = parsed.get("findings") if isinstance(parsed, dict) else None
        if not isinstance(findings, list):
            return []
        out = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            if (f.get("status") or "").lower() != "fail":
                continue
            sev = (f.get("severity") or "").upper()
            if sev not in _PREVIOUS_FAILURE_SEVERITIES:
                continue
            out.append({
                "rule": str(f.get("rule") or ""),
                "severity": sev,
                "evidence": str(f.get("evidence") or ""),
            })
            if len(out) >= limit:
                break
        return out

    def _maybe_create_sheet_row(self, attempt, verdict_norm):
        if verdict_norm == "accept":
            sheet_type = "passed"
        elif verdict_norm == "reject":
            sheet_type = "failed"
        else:
            return
        try:
            with self.env.cr.savepoint():
                self.env["crowley.sequence.sheet.row"].create_from_review(
                    self, sheet_type,
                )
        except Exception:
            _logger.exception(
                "Crowley: failed to create sequence-sheet row for review %s (type=%s)",
                self.id, sheet_type,
            )

    def _reevaluate_sheet_row(self):
        self.ensure_one()
        eff = self.effective_verdict or False
        Row = self.env["crowley.sequence.sheet.row"].sudo()
        existing = Row.search([("review_id", "=", self.id)], limit=1)
        desired_type = (
            "passed" if eff == "accept"
            else "failed" if eff == "reject"
            else False
        )
        if existing and existing.sheet_type == desired_type:
            return
        if existing:
            existing.unlink()
        if desired_type:
            try:
                with self.env.cr.savepoint():
                    Row.create_from_review(self, desired_type)
            except Exception:
                _logger.exception(
                    "Crowley: failed to re-evaluate sheet row for review %s "
                    "after human override (target type=%s)",
                    self.id, desired_type,
                )

    def _maybe_spawn_retry(self, attempt, verdict_norm):
        if verdict_norm != "reject":
            return
        icp = self.env["ir.config_parameter"].sudo()
        try:
            max_attempts = int(icp.get_param(
                "crowley.review_max_attempts",
                str(_DEFAULT_REVIEW_MAX_ATTEMPTS),
            ) or _DEFAULT_REVIEW_MAX_ATTEMPTS)
        except (TypeError, ValueError):
            max_attempts = _DEFAULT_REVIEW_MAX_ATTEMPTS
        existing = self.env["crowley.video.review"].search_count(
            [("attempt_id", "=", attempt.id)]
        )
        job = attempt.job_id
        if existing >= max_attempts:
            if job:
                job.message_post(body=_(
                    "Video review rejected — max attempts (%(n)d) reached, no auto-retry queued."
                ) % {"n": max_attempts})
            return
        new_review = self.env["crowley.video.review"].create({
            "attempt_id": attempt.id,
        })
        if job:
            job.message_post(body=_(
                "Auto-retry video review queued (attempt %(n)d of %(max)d)."
            ) % {"n": existing + 1, "max": max_attempts})
        new_review._defer("_run_review")

    @api.model
    def _cron_run_queued_reviews(self):
        icp = self.env["ir.config_parameter"].sudo()
        try:
            max_parallel = int(icp.get_param(
                "crowley.review_max_parallel",
                str(_DEFAULT_REVIEW_MAX_PARALLEL),
            ) or _DEFAULT_REVIEW_MAX_PARALLEL)
        except (TypeError, ValueError):
            max_parallel = _DEFAULT_REVIEW_MAX_PARALLEL
        try:
            batch_size = int(icp.get_param(
                "crowley.review_batch_size",
                str(_DEFAULT_REVIEW_BATCH_SIZE),
            ) or _DEFAULT_REVIEW_BATCH_SIZE)
        except (TypeError, ValueError):
            batch_size = _DEFAULT_REVIEW_BATCH_SIZE

        in_flight = self.search_count([("state", "=", _STATE_SUBMITTING)])
        capacity = max_parallel - in_flight
        if capacity <= 0:
            return
        limit = min(capacity, batch_size)

        self.env.cr.execute(
            """
            SELECT id FROM crowley_video_review
            WHERE state = %s
            ORDER BY create_date ASC, id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (_STATE_QUEUED, limit),
        )
        rows = self.env.cr.fetchall()
        if not rows:
            return
        reviews = self.browse([r[0] for r in rows])
        for rec in reviews:
            rec._defer("_run_review")
        _logger.info(
            "Crowley video-review cron: dispatched %d review(s) (in-flight=%d, max=%d)",
            len(reviews), in_flight, max_parallel,
        )

    def _apply_human_override(self, human_verdict, reason):
        self.ensure_one()
        if not self.env.user.has_group("crowley.group_crowley_manager"):
            raise ValidationError(_(
                "Only Crowley Managers can override a video review verdict."
            ))
        if self.state != _STATE_DONE:
            raise ValidationError(_(
                "Cannot override a review that has not completed (state=%(state)s)."
            ) % {"state": self.state})
        if human_verdict not in (False, "accept", "review", "reject"):
            raise ValidationError(_(
                "Invalid human verdict %(v)r; expected accept, review, reject, or empty."
            ) % {"v": human_verdict})
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise ValidationError(_(
                "An override reason is required for the audit trail."
            ))

        prior = self.human_verdict or False
        self.write({
            "human_verdict": human_verdict or False,
            "human_override_reason": clean_reason[:4000],
            "human_override_user_id": self.env.user.id,
            "human_override_at": fields.Datetime.now(),
        })

        job = self.attempt_id.job_id
        if job:
            if human_verdict:
                msg = _(
                    "Video review human verdict set: %(prior)s → %(new)s "
                    "by %(user)s. Reason: %(reason)s"
                ) % {
                    "prior": (prior or "—").upper(),
                    "new": human_verdict.upper(),
                    "user": self.env.user.display_name,
                    "reason": clean_reason[:500],
                }
            else:
                msg = _(
                    "Video review human override cleared (was %(prior)s) "
                    "by %(user)s. Reason: %(reason)s"
                ) % {
                    "prior": (prior or "—").upper(),
                    "user": self.env.user.display_name,
                    "reason": clean_reason[:500],
                }
            job.message_post(body=msg)

        self._reevaluate_sheet_row()

    def _open_override_wizard(self, mode):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Override Video Review Verdict"),
            "res_model": "crowley.review.override.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_review_id": self.id,
                "default_mode": mode,
            },
        }

    def action_override_accept(self):
        return self._open_override_wizard("accept")

    def action_override_reject(self):
        return self._open_override_wizard("reject")

    def action_override_clear(self):
        return self._open_override_wizard("clear")
