"""Leviathan HTTP controllers — webhook endpoint + API."""

import hmac
import json
import logging
import os

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# Defense in depth — cap webhook body size so a misbehaving Lambda or a
# token-stolen attacker can't OOM the HTTP worker by streaming a multi-GB
# JSON payload. Resolution order:
#   1. Odoo System Parameter `leviathan.webhook_max_bytes` (admin UI, live)
#   2. Env var LEVIATHAN_WEBHOOK_MAX_BYTES (devops, restart-required)
#   3. Hard default 10 MB
# Read fresh per request so admin can adjust without pod restart.
_WEBHOOK_MAX_BYTES_DEFAULT = 10 * 1024 * 1024


def _get_webhook_max_bytes():
    """ICP > env > default. Called once per webhook request."""
    try:
        if request and request.env:
            v = request.env["ir.config_parameter"].sudo().get_param(
                "leviathan.webhook_max_bytes"
            )
            if v:
                return int(v)
    except Exception:
        pass
    return int(os.environ.get(
        "LEVIATHAN_WEBHOOK_MAX_BYTES",
        str(_WEBHOOK_MAX_BYTES_DEFAULT),
    ))


def _verify_webhook_token():
    """Verify the X-Leviathan-Token header against the shared secret.

    ``LEVIATHAN_WEBHOOK_TOKEN`` accepts a comma-separated list of tokens
    (``"prod,old,new"``) so the secret can be rotated without a pod
    restart: deploy with both old and new accepted, switch the Lambda to
    the new one, then drop the old from the list on the next deploy. Each
    token is compared in constant time via ``hmac.compare_digest``.
    """
    secret_env = os.environ.get("LEVIATHAN_WEBHOOK_TOKEN", "")
    if not secret_env:
        _logger.error(
            "LEVIATHAN_WEBHOOK_TOKEN not set -- rejecting webhook. "
            "Set this env var in production!"
        )
        return False
    received = request.httprequest.headers.get("X-Leviathan-Token") or ""
    accepted = [t.strip() for t in secret_env.split(",") if t.strip()]
    if not accepted:
        return False
    return any(hmac.compare_digest(received, t) for t in accepted)


class LeviathanController(http.Controller):

    @http.route(
        "/api/v1/leviathan/webhook/extraction-complete",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook_extraction_complete(self, **kwargs):
        """Webhook called by the external extraction service when a job finishes."""
        if not _verify_webhook_token():
            _logger.warning(
                "Webhook auth failed — invalid X-Leviathan-Token"
            )
            return Response(
                json.dumps({"error": "Unauthorized"}),
                status=401,
                content_type="application/json",
            )

        # Cap payload size — defense in depth against a misbehaving
        # upstream streaming a multi-GB body. Werkzeug enforces this at
        # the request level too; we re-check at app level so the cap is
        # visible in code and tunable per environment.
        max_bytes = _get_webhook_max_bytes()
        declared_len = request.httprequest.content_length
        if isinstance(declared_len, int) and declared_len > max_bytes:
            _logger.warning(
                "Webhook rejected: Content-Length %d > cap %d",
                declared_len, max_bytes,
            )
            return Response(
                json.dumps({"error": "Payload too large"}),
                status=413,
                content_type="application/json",
            )
        body = request.httprequest.data
        if isinstance(body, (bytes, bytearray)) and len(body) > max_bytes:
            _logger.warning(
                "Webhook rejected: body length %d > cap %d",
                len(body), max_bytes,
            )
            return Response(
                json.dumps({"error": "Payload too large"}),
                status=413,
                content_type="application/json",
            )

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return Response(
                json.dumps({"error": "Invalid JSON body"}),
                status=400,
                content_type="application/json",
            )

        job_id = data.get("job_id")
        if not job_id:
            return Response(
                json.dumps({"error": "Missing job_id"}),
                status=400,
                content_type="application/json",
            )

        record = request.env["leviathan.job"].sudo().browse(int(job_id))
        if not record.exists():
            return Response(
                json.dumps({"error": f"Job {job_id} not found"}),
                status=404,
                content_type="application/json",
            )

        # Lightweight "extraction started" ping — the Lambda sends this when it
        # actually picks the job up. Update last_heartbeat so the watchdog
        # measures real progress, not time-since-state-change (a job merely
        # queued in AWS shouldn't be killed for being slow to start).
        if data.get("status") == "started":
            if record.state == "extracting":
                record.write({"last_heartbeat": fields.Datetime.now()})
                _logger.info("[leviathan][job=%s] extraction started ping", record.name)
            return Response(
                json.dumps({"status": "ack"}),
                status=200,
                content_type="application/json",
            )

        if record.state != "extracting":
            _logger.info(
                "Webhook for job %s ignored: state is '%s' "
                "(expected 'extracting') -- idempotency guard",
                job_id,
                record.state,
            )
            return Response(
                json.dumps({
                    "ignored": True,
                    "reason": f"Job in state '{record.state}' (already processed)",
                }),
                status=200,
                content_type="application/json",
            )

        if record.cancel_requested:
            _logger.info(
                "Webhook for job %s ignored: cancel_requested=True", job_id
            )
            return Response(
                json.dumps({"ignored": True, "reason": "Job was cancelled"}),
                status=200,
                content_type="application/json",
            )

        try:
            success = data.get("success")
            if not success:
                error_msg = data.get("error", "Extraction failed (no details)")
                record._mark_failed(error_msg)
                return Response(
                    json.dumps({"status": "failed"}),
                    status=200,
                    content_type="application/json",
                )

            site_discovery = data.get("site_discovery")
            prd_prompt = data.get("prd_prompt")
            artifacts = data.get("artifacts")
            screenshot_keys = data.get("screenshot_keys", [])
            asset_keys = data.get("asset_keys", [])
            is_partial = data.get("partial", False)
            warnings = data.get("warnings") or []

            ICP = request.env["ir.config_parameter"].sudo()
            s3_bucket = ICP.get_param("leviathan.s3_bucket")
            s3_key_id = ICP.get_param("leviathan.s3_access_key_id")
            s3_secret = ICP.get_param("leviathan.s3_secret_access_key")
            s3_region = ICP.get_param("leviathan.s3_region") or "us-east-1"
            s3_folder = ICP.get_param("leviathan.s3_folder") or "leviathan"
            cdn_url = ICP.get_param("leviathan.s3_cdn_url")

            # Defer the multi-MB artifacts upload to a background thread so
            # the webhook returns in <50 ms. Under a 250-callback burst this
            # is the difference between Lambda's callback budget surviving
            # vs. ALB queue fill -> webhook timeout -> Lambda retries (which
            # we disabled at the Lambda config level) or stranded jobs in
            # `extracting` state. PRD generation doesn't read artifacts_url,
            # so the two run concurrently. See model._upload_artifacts_bg
            # for failure semantics.
            artifacts_pending = bool(artifacts and s3_bucket)
            s3_config = {
                "bucket": s3_bucket,
                "key_id": s3_key_id,
                "secret": s3_secret,
                "region": s3_region,
                "folder": s3_folder,
                "cdn_url": cdn_url,
            }

            write_vals = {
                "prd_prompt": prd_prompt,
                "last_heartbeat": fields.Datetime.now(),
            }

            if site_discovery:
                title = site_discovery.get("title")
                tech_stack = site_discovery.get("tech_stack")
                pages = site_discovery.get("pages")
                if title:
                    write_vals["site_name"] = title
                if tech_stack:
                    write_vals["tech_stack"] = json.dumps(tech_stack) if isinstance(tech_stack, dict) else str(tech_stack)
                if pages:
                    write_vals["page_count"] = len(pages)
                write_vals["site_discovery_json"] = site_discovery

            if screenshot_keys:
                write_vals["screenshot_keys"] = screenshot_keys
            if asset_keys:
                write_vals["asset_keys"] = asset_keys
            # Surface partial/warnings WITHOUT polluting error_message — a
            # successful-but-imperfect extraction is not a red failure. The
            # tasker sees a non-red "Partial extraction" banner instead.
            if warnings or is_partial:
                summary = warnings or ["Extraction was partial (deadline reached)."]
                write_vals["extraction_warnings"] = (
                    "\n".join(f"• {w}" for w in summary)[:1000]
                )
                _logger.info(
                    "[leviathan][job=%s] extraction succeeded with warnings: %s",
                    record.name, summary,
                )
            else:
                write_vals["extraction_warnings"] = False

            # Full transparency: persist the complete Lambda callback. Artifacts
            # are trimmed to filenames — their content is large and already on S3.
            callback_snapshot = dict(data)
            if isinstance(callback_snapshot.get("artifacts"), dict):
                callback_snapshot["artifacts"] = sorted(callback_snapshot["artifacts"].keys())
            write_vals["lambda_callback_json"] = callback_snapshot

            write_vals["state"] = "generating"
            record.write(write_vals)

            # Notify browser of state change
            try:
                request.env["bus.bus"]._sendone(
                    "leviathan_job_updates",
                    "leviathan/job_state",
                    {"id": record.id, "state": "generating"},
                )
            except Exception:
                pass

            # Use postcommit to ensure data is committed before
            # background threads read it (fixes race condition LG-3).
            db_name = request.env.cr.dbname
            record_id = record.id
            # Capture artifacts off the request — the closure executes
            # post-commit, and the request scope is gone by then.
            artifacts_for_bg = artifacts if artifacts_pending else None

            def _deferred():
                from ..models.leviathan_job import _submit_bg
                # 1) Artifacts upload to S3 — async, must not block webhook
                if artifacts_for_bg:
                    _submit_bg(
                        f"artifacts-upload[job={record_id}]",
                        record._upload_artifacts_bg,
                        db_name, record_id, artifacts_for_bg, s3_config,
                    )
                # 2) PRD generation — runs concurrently with the upload
                _submit_bg(
                    f"prd-gen[job={record_id}]",
                    record._run_prd_generation_bg, db_name, record_id,
                )

            request.env.cr.postcommit.add(_deferred)

            return Response(
                json.dumps({"status": "success", "next_step": "generating"}),
                status=200,
                content_type="application/json",
            )

        except Exception as exc:
            _logger.exception(
                "Webhook processing failed for job %s", job_id
            )
            record._mark_failed(str(exc))
            return Response(
                json.dumps({"error": "Internal server error"}),
                status=500,
                content_type="application/json",
            )

    @http.route(
        "/api/v1/leviathan/jobs/<int:job_id>/status",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def get_job_status(self, job_id, **kwargs):
        """Job status API."""
        try:
            record = request.env["leviathan.job"].browse(job_id)
            if not record.exists():
                return Response(
                    json.dumps({"error": "Job not found"}),
                    status=404,
                    content_type="application/json",
                )

            result = {
                "id": record.id,
                "name": record.name,
                "url": record.url,
                "state": record.state,
                "category": record.category_id.name if record.category_id else None,
                "score": record.score,
                "grade": record.grade,
                "qc_verdict": record.qc_verdict,
                "prd_url": record.prd_url,
                "duration_seconds": record.duration_seconds,
                "llm_attempts": record.llm_attempts,
                "error_message": record.error_message,
                "started_at": record.started_at.isoformat() if record.started_at else None,
                "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            }
            return Response(
                json.dumps({"result": result}),
                status=200,
                content_type="application/json",
            )
        except Exception as exc:
            _logger.exception("Job status API failed for %s", job_id)
            return Response(
                json.dumps({"error": "Internal server error"}),
                status=500,
                content_type="application/json",
            )
