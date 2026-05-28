from __future__ import annotations

import json
import logging
import traceback

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import credential_manager
from ..services import (
    auto_repair,
    enrichment_client,
    template_fallback,
    validator as validator_svc,
)

_TEMPERATURE_SCHEDULE = (0.8, 0.4, 0.15)

_logger = logging.getLogger(__name__)

_STATE_QUEUED = "queued"
_STATE_SUBMITTING = "submitting"
_STATE_VALIDATING = "validating"
_STATE_CLEAN = "clean"
_STATE_WARNED = "warned"
_STATE_FATAL = "fatal"
_STATE_ERROR = "error"

_NON_TERMINAL = {_STATE_QUEUED, _STATE_SUBMITTING, _STATE_VALIDATING}
_TERMINAL = {_STATE_CLEAN, _STATE_WARNED, _STATE_FATAL, _STATE_ERROR}

_ALLOWED_TRANSITIONS = {
    _STATE_QUEUED:     {_STATE_SUBMITTING, _STATE_ERROR},
    _STATE_SUBMITTING: {_STATE_VALIDATING, _STATE_ERROR},
    _STATE_VALIDATING: {_STATE_CLEAN, _STATE_WARNED, _STATE_FATAL, _STATE_ERROR},
    _STATE_CLEAN:      set(),
    _STATE_WARNED:     set(),
    _STATE_FATAL:      set(),
    _STATE_ERROR:      set(),
}

_STYLE_DEFAULT = "precise"
_PRIORITY_DEFAULT = "medium"
_COMPLEXITY_DEFAULT = "moderate"


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


class T2AVEnrichment(models.Model):
    _name = "t2av.enrichment"
    _description = "T2AV Prompt Enrichment Attempt"
    _order = "job_id, attempt_number desc"
    _rec_name = "display_name"

    job_id = fields.Many2one(
        "t2av.generation", string="Job",
        required=True, ondelete="cascade", index=True,
    )
    attempt_number = fields.Integer(string="Attempt #", required=True, copy=False)
    display_name = fields.Char(compute="_compute_display_name", store=False)

    model_id = fields.Char(
        string="Model ID", readonly=True, copy=False,
    )
    model_id_display = fields.Char(
        string="Model", compute="_compute_model_id_display",
        help="Model identifier with AWS account ID masked for safe UI display.",
    )
    region = fields.Char(string="AWS Region", readonly=True, copy=False)
    bedrock_request_id = fields.Char(
        string="Bedrock Request ID", readonly=True, copy=False,
        help="AWS Bedrock x-amzn-RequestId — provide to AWS support when filing a ticket.",
    )

    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("submitting", "Submitting"),
            ("validating", "Validating"),
            ("clean", "Clean"),
            ("warned", "Warned"),
            ("fatal", "Fatal (validator)"),
            ("error", "Error"),
        ],
        string="Status", default=_STATE_QUEUED, readonly=True,
        copy=False, index=True,
    )

    cost_usd = fields.Float(
        string="Cost (USD)", digits=(12, 6), readonly=True, copy=False,
        compute="_compute_cost_usd", store=True,
        help="Bedrock Claude 3.5 Sonnet pricing: $3/M input tokens, $15/M output tokens.",
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
            ("state", "in", (_STATE_QUEUED, _STATE_SUBMITTING, _STATE_VALIDATING)),
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
                    _logger.exception("T2AV enrichment watchdog: failed on id=%s", rec.id)
        if stuck:
            _logger.info("T2AV enrichment watchdog: reset %d stuck row(s)", len(stuck))

    enriched_text = fields.Text(string="Enriched Prompt", readonly=True, copy=False)
    word_count = fields.Integer(string="Word Count", readonly=True, copy=False)
    validator_passed = fields.Boolean(string="Validator Passed", readonly=True, copy=False)
    validator_warnings = fields.Text(string="Validator Warnings (JSON)", readonly=True, copy=False)
    validator_fatals = fields.Text(string="Validator Fatals (JSON)", readonly=True, copy=False)

    submitted_at = fields.Datetime(string="Submitted At", readonly=True, copy=False)
    completed_at = fields.Datetime(string="Completed At", readonly=True, copy=False)
    input_tokens = fields.Integer(string="Input Tokens", readonly=True, copy=False)
    output_tokens = fields.Integer(string="Output Tokens", readonly=True, copy=False)
    tokens_used = fields.Integer(
        string="Tokens Used", compute="_compute_tokens_used", store=True,
    )

    error_message = fields.Text(string="Error Message", readonly=True, copy=False)
    error_code = fields.Char(string="Error Code", readonly=True, copy=False)

    _unique_per_job = models.Constraint(
        "UNIQUE(job_id, attempt_number)",
        message="Enrichment attempt number must be unique within a job.",
    )
    _number_positive = models.Constraint(
        "CHECK(attempt_number >= 1)",
        message="Enrichment attempt number must be at least 1.",
    )

    @api.depends("job_id.name", "attempt_number")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = (
                f"{rec.job_id.name or 'CRW/?'} · enrich {rec.attempt_number or '?'}"
            )

    @api.depends("model_id")
    def _compute_model_id_display(self):
        for rec in self:
            rec.model_id_display = _mask_model_id(rec.model_id)

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
                        "Cannot transition enrichment %(name)s from %(old)s to %(new)s."
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
                    rec = env["t2av.enrichment"].browse(rec_id).exists()
                    if rec:
                        getattr(rec, callback_name)(*args)
            except Exception:
                _logger.exception(
                    "T2AV enrichment deferred task failed (id=%s, cb=%s)",
                    rec_id, callback_name,
                )
                try:
                    registry = odoo.modules.registry.Registry(db_name)
                    with registry.cursor() as cr2:
                        env2 = odoo.api.Environment(cr2, uid, {})
                        rec = env2["t2av.enrichment"].browse(rec_id).exists()
                        if rec and rec.state in _NON_TERMINAL:
                            rec.write({
                                "state": _STATE_ERROR,
                                "error_code": "internal_error",
                                "error_message": (traceback.format_exc() or "")[:8000],
                                "completed_at": fields.Datetime.now(),
                            })
                except Exception:
                    _logger.exception(
                        "T2AV: failed to mark enrichment %s as error", rec_id,
                    )

        self.env.cr.postcommit.add(_run)

    def _run_enrich(self):
        self.ensure_one()
        if self.state != _STATE_QUEUED:
            return

        job = self.job_id
        if not job.exists():
            self._fail("no_job", "Parent job no longer exists.")
            return

        bedrock_api_key = credential_manager.get_bedrock_api_key(self.env)
        access_key = ""
        secret_key = ""
        if not bedrock_api_key:
            access_key = credential_manager.get_aws_access_key(self.env)
            secret_key = credential_manager.get_aws_secret_key(self.env)
            if not access_key or not secret_key:
                self._fail(
                    "aws_creds_missing",
                    "Bedrock auth missing: set either (a) Bedrock API Key "
                    "(starts with ABSK...) OR (b) AWS Access Key + Secret Key "
                    "in Settings > T2AV.",
                )
                return

        icp = self.env["ir.config_parameter"].sudo()
        model_id = icp.get_param(
            "t2av.bedrock_model_id", enrichment_client.DEFAULT_MODEL_ID,
        )
        region = icp.get_param(
            "t2av.bedrock_region", enrichment_client.DEFAULT_REGION,
        )
        try:
            max_attempts = int(icp.get_param("t2av.bedrock_max_retries", "5"))
        except (TypeError, ValueError):
            max_attempts = 5

        self.write({
            "state": _STATE_SUBMITTING,
            "submitted_at": fields.Datetime.now(),
            "model_id": model_id,
            "region": region,
        })

        metadata = self._build_metadata(job)

        previous_failures = []
        if self.attempt_number > 1:
            prior_fatal = job.enrichment_ids.filtered(
                lambda r: r.attempt_number < self.attempt_number
                and r.state == _STATE_FATAL
                and r.validator_fatals
            ).sorted("attempt_number", reverse=True)
            if prior_fatal:
                try:
                    previous_failures = json.loads(
                        prior_fatal[0].validator_fatals or "[]"
                    ) or []
                except (json.JSONDecodeError, TypeError):
                    previous_failures = []

        attempt_temperature = _TEMPERATURE_SCHEDULE[
            min(max(self.attempt_number - 1, 0), len(_TEMPERATURE_SCHEDULE) - 1)
        ]

        try:
            result = enrichment_client.enrich(
                access_key=access_key,
                secret_key=secret_key,
                bedrock_api_key=bedrock_api_key,
                region=region,
                model_id=model_id,
                metadata=metadata,
                previous_failures=previous_failures,
                max_attempts=max_attempts,
                temperature=attempt_temperature,
            )
        except enrichment_client.EnrichmentAuthError as e:
            self._fail("aws_auth", str(e))
            return
        except enrichment_client.EnrichmentConfigError as e:
            self._fail("config", str(e))
            return
        except enrichment_client.EnrichmentError as e:
            self._fail("bedrock_error", str(e))
            return

        enriched_text = result["text"]
        request_id = result.get("request_id", "")
        stop_reason = result.get("stop_reason", "")

        if not enriched_text:
            self.write({"bedrock_request_id": request_id})
            self._fail("empty_output", "LLM returned an empty response.")
            return

        self.write({
            "state": _STATE_VALIDATING,
            "enriched_text": enriched_text,
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "bedrock_request_id": request_id,
        })

        if stop_reason and stop_reason != "end_turn":
            self.write({
                "state": _STATE_FATAL,
                "validator_passed": False,
                "validator_fatals": json.dumps([{
                    "rule": "BEDROCK_TRUNCATED",
                    "severity": "FATAL",
                    "message": (
                        f"Bedrock terminated with stop_reason={stop_reason!r}. "
                        "Output likely truncated. Increase max_tokens or "
                        "shorten the source prompt."
                    ),
                    "evidence": (enriched_text or "")[:200],
                }]),
                "completed_at": fields.Datetime.now(),
            })
            job.message_post(body=_(
                "Enrichment #%(n)d FAILED: Bedrock stop_reason=%(reason)s "
                "(output likely truncated)."
            ) % {"n": self.attempt_number, "reason": stop_reason})
            return

        try:
            report = validator_svc.validate(
                enriched_text,
                style=getattr(job, "style", None) or _STYLE_DEFAULT,
                category=job.category,
            )
        except Exception as e:
            _logger.exception("T2AV enrichment: validator crashed")
            self._fail("validator_crash", str(e))
            return

        bucket = validator_svc.categorize(report)
        warnings_json = validator_svc.serialize_findings(report.warnings)
        fatals_json = validator_svc.serialize_findings(report.fatal)

        repair_source = "llm"
        if bucket in ("fatal", "warned"):
            try:
                repaired_text, repair_applied = auto_repair.repair_all(
                    enriched_text,
                    category=job.category or "",
                    style=getattr(job, "style", None) or _STYLE_DEFAULT,
                )
                if repair_applied and repaired_text != enriched_text:
                    repaired_report = validator_svc.validate(
                        repaired_text,
                        style=getattr(job, "style", None) or _STYLE_DEFAULT,
                        category=job.category,
                    )
                    repaired_bucket = validator_svc.categorize(repaired_report)
                    if repaired_bucket == "clean":
                        enriched_text = repaired_text
                        report = repaired_report
                        bucket = repaired_bucket
                        warnings_json = validator_svc.serialize_findings(report.warnings)
                        fatals_json = validator_svc.serialize_findings(report.fatal)
                        repair_source = "auto_repair"
                        fixes = "; ".join(repair_applied[:5])
                        job.message_post(body=_(
                            "Tier 1 auto-repair saved enrichment #%(n)d: %(fixes)s"
                        ) % {"n": self.attempt_number, "fixes": fixes})
            except Exception:
                _logger.exception(
                    "T2AV: auto_repair crashed on job %s; falling through unchanged",
                    job.id,
                )

        new_state = {
            "clean": _STATE_CLEAN,
            "warned": _STATE_WARNED,
            "fatal": _STATE_FATAL,
        }[bucket]

        self.write({
            "state": new_state,
            "enriched_text": enriched_text,
            "word_count": report.word_count,
            "validator_passed": report.passed,
            "validator_warnings": warnings_json or "",
            "validator_fatals": fatals_json or "",
            "completed_at": fields.Datetime.now(),
        })

        if new_state == _STATE_CLEAN:
            job.write({
                "enriched_prompt": enriched_text,
                "golden_prompt": enriched_text,
                "golden_source": repair_source,
            })
            label = (
                _("CLEAN")
                if repair_source == "llm"
                else _("CLEAN (auto-repaired)")
            )
            job.message_post(body=_(
                "Enrichment #%(n)d %(label)s — Golden Prompt set (%(words)d words)."
            ) % {"n": self.attempt_number, "label": label, "words": report.word_count})
        elif new_state == _STATE_WARNED:
            job.write({"enriched_prompt": enriched_text, "golden_prompt": False})
            warn_rules = ", ".join(f.rule for f in report.warnings[:5])
            job.message_post(body=_(
                "Enrichment #%(n)d WARNED — enriched text saved but NOT Golden. "
                "Warnings: %(rules)s"
            ) % {"n": self.attempt_number, "rules": warn_rules})
        else:
            rules = ", ".join(f.rule for f in report.fatal[:5])
            job.message_post(body=_(
                "Enrichment #%(n)d FAILED validator: %(rules)s"
            ) % {"n": self.attempt_number, "rules": rules})

            try:
                max_enrichment_attempts = int(
                    icp.get_param("t2av.enrichment_max_attempts", "3")
                )
            except (TypeError, ValueError):
                max_enrichment_attempts = 3

            if self.attempt_number < max_enrichment_attempts:
                existing_numbers = job.enrichment_ids.mapped("attempt_number") or [0]
                next_n = max(existing_numbers) + 1
                try:
                    retry = self.env["t2av.enrichment"].create({
                        "job_id": job.id,
                        "attempt_number": next_n,
                        "state": _STATE_QUEUED,
                    })
                    job.message_post(body=_(
                        "Auto-regenerating with failure feedback "
                        "(attempt #%(n)d / max %(max)d)."
                    ) % {"n": next_n, "max": max_enrichment_attempts})
                    if not self.env.context.get("t2av_inline_pipeline"):
                        retry._defer("_run_enrich")
                except Exception:
                    _logger.exception(
                        "T2AV: auto-retry failed after FATAL on job %s",
                        job.id,
                    )
            else:
                job.message_post(body=_(
                    "Max %d enrichment attempts reached; no further auto-retry."
                ) % max_enrichment_attempts)
                try:
                    fallback_text = template_fallback.build(
                        metadata=metadata,
                        style=getattr(job, "style", None) or _STYLE_DEFAULT,
                    )
                    fallback_report = validator_svc.validate(
                        fallback_text,
                        style=getattr(job, "style", None) or _STYLE_DEFAULT,
                        category=job.category,
                    )
                    if validator_svc.categorize(fallback_report) != "fatal":
                        job.write({
                            "enriched_prompt": fallback_text,
                            "golden_prompt": fallback_text,
                            "golden_source": "template_fallback",
                        })
                        job.message_post(body=_(
                            "Tier 3 template fallback engaged: Golden Prompt "
                            "set via deterministic per-category template "
                            "(%(words)d words)."
                        ) % {"words": fallback_report.word_count})
                    else:
                        _logger.error(
                            "T2AV: template_fallback produced FATAL output for "
                            "job %s, category=%s, style=%s. Golden Prompt NOT set.",
                            job.id, job.category, getattr(job, "style", None),
                        )
                except Exception:
                    _logger.exception(
                        "T2AV: template_fallback crashed on job %s; "
                        "Golden Prompt NOT set.",
                        job.id,
                    )

    def _build_metadata(self, job):
        return {
            "Category": job.category or "",
            "Sub_Category": getattr(job, "sub_category", "") or "",
            "Style": (getattr(job, "style", "") or _STYLE_DEFAULT).title(),
            "Priority": (getattr(job, "priority", "") or _PRIORITY_DEFAULT).title(),
            "Topic": getattr(job, "topic", "") or "",
            "Complexity": (getattr(job, "complexity", "") or _COMPLEXITY_DEFAULT).title(),
            "Prompt": (job.prompt or "").strip(),
        }

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
        self.job_id.message_post(body=_(
            "Enrichment #%(n)d errored (%(code)s): %(msg)s"
        ) % {
            "n": self.attempt_number, "code": error_code,
            "msg": (error_message or "")[:200],
        })
