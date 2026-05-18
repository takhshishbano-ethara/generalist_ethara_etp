"""Gohan HTTP controllers — webhook endpoint + API."""

import hashlib
import hmac
import json
import logging
import os

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _verify_webhook_token():
    """Verify the X-Gohan-Token header against the shared secret.

    If GOHAN_WEBHOOK_TOKEN env var is not set, auth is bypassed with a
    warning.  In production the env var MUST be set — deployment scripts
    enforce this.
    """
    secret = os.environ.get("GOHAN_WEBHOOK_TOKEN")
    if not secret:
        _logger.error(
            "GOHAN_WEBHOOK_TOKEN not set -- rejecting webhook. "
            "Set this env var in production!"
        )
        return False
    token = request.httprequest.headers.get("X-Gohan-Token")
    return hmac.compare_digest(token or "", secret)


def _verify_hmac_signature(raw_body: bytes) -> bool:
    """Verify the X-Gohan-Signature HMAC-SHA256 header against ``gohan.hmac_secret``.

    Constant-time comparison via :func:`hmac.compare_digest` to avoid
    timing attacks. The shared secret comes from the system parameter,
    NOT the OS environment, so it can be rotated without redeploying
    Odoo. If the secret is unset, the webhook is rejected.
    """
    secret = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("gohan.hmac_secret")
    )
    if not secret:
        _logger.error(
            "[gohan][webhook] gohan.hmac_secret not configured -- rejecting "
            "spec webhook. Set it under Settings -> Gohan -> API Gateway."
        )
        return False
    provided = request.httprequest.headers.get("X-Gohan-Signature", "") or ""
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


class GohanController(http.Controller):

    @http.route(
        "/api/v1/gohan/webhook/extraction-complete",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook_extraction_complete(self, **kwargs):
        """Webhook called by the external extraction service when a job finishes."""
        req = request.httprequest
        body_len = len(req.data or b"")
        _logger.info(
            "[gohan][diag][webhook] HIT extraction-complete: remote=%s "
            "method=%s ua=%r token_present=%s body_bytes=%d",
            req.remote_addr,
            req.method,
            req.headers.get("User-Agent", ""),
            bool(req.headers.get("X-Gohan-Token")),
            body_len,
        )
        if not _verify_webhook_token():
            _logger.warning(
                "[gohan][diag][webhook] AUTH FAILED — token_env_set=%s "
                "token_header_set=%s. Set GOHAN_WEBHOOK_TOKEN env var on Odoo "
                "AND ensure the Lambda sends the same value as X-Gohan-Token.",
                bool(os.environ.get("GOHAN_WEBHOOK_TOKEN")),
                bool(req.headers.get("X-Gohan-Token")),
            )
            return Response(
                json.dumps({"error": "Unauthorized"}),
                status=401,
                content_type="application/json",
            )

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, ValueError):
            _logger.warning(
                "[gohan][diag][webhook] invalid JSON body (first 300 bytes): %r",
                (request.httprequest.data or b"")[:300],
            )
            return Response(
                json.dumps({"error": "Invalid JSON body"}),
                status=400,
                content_type="application/json",
            )

        job_id = data.get("job_id")
        _logger.info(
            "[gohan][diag][webhook] parsed: job_id=%s status=%s success=%s keys=%s",
            job_id, data.get("status"), data.get("success"), list(data.keys()),
        )
        if not job_id:
            return Response(
                json.dumps({"error": "Missing job_id"}),
                status=400,
                content_type="application/json",
            )

        record = request.env["gohan.job"].sudo().browse(int(job_id))
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
                _logger.info("[gohan][job=%s] extraction started ping", record.name)
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
            s3_bucket = ICP.get_param("gohan.s3_bucket")
            s3_key_id = ICP.get_param("gohan.s3_access_key_id")
            s3_secret = ICP.get_param("gohan.s3_secret_access_key")
            s3_region = ICP.get_param("gohan.s3_region") or "us-east-1"
            s3_folder = ICP.get_param("gohan.s3_folder") or "gohan"
            cdn_url = ICP.get_param("gohan.s3_cdn_url")

            artifacts_url = None
            if artifacts and s3_bucket:
                from ..services.s3_service import (
                    upload_artifacts_to_s3,
                    get_artifacts_folder_url,
                )

                upload_artifacts_to_s3(
                    artifacts=artifacts,
                    job_name=record.name,
                    bucket=s3_bucket,
                    access_key_id=s3_key_id,
                    secret_key=s3_secret,
                    region=s3_region,
                    folder=s3_folder,
                    cdn_url=cdn_url,
                )
                artifacts_url = get_artifacts_folder_url(
                    job_name=record.name,
                    bucket=s3_bucket,
                    folder=s3_folder,
                    cdn_url=cdn_url,
                )

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

            if artifacts_url:
                write_vals["artifacts_url"] = artifacts_url
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
                    "[gohan][job=%s] extraction succeeded with warnings: %s",
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
                    "gohan_job_updates",
                    "gohan/job_state",
                    {"id": record.id, "state": "generating"},
                )
            except Exception:
                pass

            # Use postcommit to ensure data is committed before
            # background thread reads it (fixes race condition LG-3)
            db_name = request.env.cr.dbname
            record_id = record.id

            def _deferred():
                from ..models.gohan_job import _submit_bg
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
        "/gohan/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_spec(self, **kwargs):
        """Spec-aligned run-complete webhook (HANDOFF_ODOO.md §4.5).

        Auth: ``X-Gohan-Signature`` header = hex HMAC-SHA256 of the raw
        request body using ``gohan.hmac_secret``. Constant-time verified.

        Body (JSON)::

            {
              "run_id": 42,
              "status": "done" | "failed",
              "score": 96.5,
              "qc_verdict": "shippable" | "not_shippable" | "pending",
              "eq_tier": "AUTHENTICATED" | "API_DOCS"
                       | "MARKETING_RICH" | "MARKETING_ONLY",
              "s3_artifact_prefix": "s3://gohan-artifacts/runs/42/",
              "lambda_request_id": "abc-123-def",
              "error_message": "..."   // failed only
            }

        ``run_id`` is treated as the ``gohan.job.id``. On ``done`` the job
        is finalized with the spec payload; on ``failed`` the error
        message is recorded and state flips to ``failed``.
        """
        raw_body = request.httprequest.data or b""
        if not _verify_hmac_signature(raw_body):
            _logger.warning(
                "[gohan][webhook] HMAC verification failed -- rejecting"
            )
            return Response(
                json.dumps({"error": "invalid signature"}),
                status=401,
                content_type="application/json",
            )

        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return Response(
                json.dumps({"error": "invalid JSON body"}),
                status=400,
                content_type="application/json",
            )

        run_id = body.get("run_id")
        status = body.get("status")
        if not run_id or status not in ("done", "failed"):
            return Response(
                json.dumps({
                    "error": (
                        "missing run_id or status (must be 'done' or 'failed')"
                    )
                }),
                status=400,
                content_type="application/json",
            )

        try:
            run_id_int = int(run_id)
        except (TypeError, ValueError):
            return Response(
                json.dumps({"error": "run_id must be an integer"}),
                status=400,
                content_type="application/json",
            )

        job = request.env["gohan.job"].sudo().browse(run_id_int)
        if not job.exists():
            return Response(
                json.dumps({"error": f"run {run_id_int} not found"}),
                status=404,
                content_type="application/json",
            )

        now = fields.Datetime.now()
        if status == "done":
            job.write({
                "state": "done",
                "score": float(body.get("score") or 0.0),
                "qc_verdict": body.get("qc_verdict") or "pending",
                "eq_tier": body.get("eq_tier") or False,
                "s3_artifact_prefix": body.get("s3_artifact_prefix") or False,
                "lambda_request_id": body.get("lambda_request_id") or False,
                "completed_at": now,
                "last_heartbeat": now,
                "lambda_callback_json": body,
            })
            try:
                job._notify_state_change("done")
            except Exception:
                _logger.exception(
                    "[gohan][webhook][job=%s] _notify_state_change failed",
                    job.name,
                )
            _logger.info(
                "[gohan][webhook][job=%s] finalized via /gohan/webhook "
                "(score=%s, qc=%s, eq=%s)",
                job.name,
                body.get("score"),
                body.get("qc_verdict"),
                body.get("eq_tier"),
            )
        else:
            error_msg = body.get("error_message") or "Pipeline reported failure"
            job.write({
                "state": "failed",
                "error_message": str(error_msg)[:500],
                "lambda_request_id": body.get("lambda_request_id") or False,
                "completed_at": now,
                "last_heartbeat": now,
                "lambda_callback_json": body,
            })
            try:
                job._notify_state_change("failed")
            except Exception:
                _logger.exception(
                    "[gohan][webhook][job=%s] _notify_state_change failed",
                    job.name,
                )
            _logger.warning(
                "[gohan][webhook][job=%s] marked failed via /gohan/webhook: %s",
                job.name, error_msg,
            )

        return Response(
            json.dumps({"status": "ok"}),
            status=200,
            content_type="application/json",
        )

    @http.route(
        "/api/v1/gohan/jobs/<int:job_id>/status",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def get_job_status(self, job_id, **kwargs):
        """Job status API."""
        try:
            record = request.env["gohan.job"].browse(job_id)
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
