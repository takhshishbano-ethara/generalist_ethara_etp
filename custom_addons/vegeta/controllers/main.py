"""Vegeta HTTP controllers — webhook endpoint + API."""

import hmac
import json
import logging
import os

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _verify_webhook_token():
    """Resolve shared secret with ICP > env-var precedence.

    Settings > Vegeta > Webhook Token is the source of truth so secrets can
    rotate without a process restart. Env vars VEGETA_WEBHOOK_TOKEN /
    LEVIATHAN_WEBHOOK_TOKEN remain as fallback for legacy / EKS deployments
    that pre-date the Settings field.
    """
    icp = request.env["ir.config_parameter"].sudo().get_param("vegeta.webhook_token", "")
    secret = (
        icp
        or os.environ.get("VEGETA_WEBHOOK_TOKEN")
        or os.environ.get("LEVIATHAN_WEBHOOK_TOKEN")
    )
    if not secret:
        _logger.error(
            "No webhook token configured -- rejecting. Set Settings > Vegeta > "
            "Webhook Token, or VEGETA_WEBHOOK_TOKEN env var."
        )
        return False
    headers = request.httprequest.headers
    token = headers.get("X-Vegeta-Token") or headers.get("X-Leviathan-Token")
    return hmac.compare_digest(token or "", secret)


class VegetaController(http.Controller):

    @http.route(
        "/api/v1/vegeta/webhook/extraction-complete",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook_extraction_complete(self, **kwargs):
        """Webhook called by the external extraction service when a job finishes."""
        if not _verify_webhook_token():
            _logger.warning(
                "Webhook auth failed — invalid X-Vegeta-Token"
            )
            return Response(
                json.dumps({"error": "Unauthorized"}),
                status=401,
                content_type="application/json",
            )

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, ValueError, TypeError):
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

        try:
            job_id_int = int(job_id)
        except (TypeError, ValueError):
            return Response(
                json.dumps({"error": "Invalid job_id format (must be integer)"}),
                status=400,
                content_type="application/json",
            )

        record = request.env["vegeta.job"].sudo().browse(job_id_int)
        if not record.exists():
            return Response(
                json.dumps({"error": f"Job {job_id} not found"}),
                status=404,
                content_type="application/json",
            )

        # Diagnostic: log every inbound webhook with enough context to
        # reconstruct the extraction timeline. `age` is how long the job has
        # been in `extracting` since dispatch — the single most useful number
        # when debugging "stuck in extracting": a large age on the FINAL
        # callback means the Lambda sat in AWS's async-invocation queue
        # (reserved-concurrency exhaustion), not that extraction was slow.
        _now = fields.Datetime.now()
        _age = (
            (_now - record.started_at).total_seconds()
            if record.started_at else -1
        )
        _logger.info(
            "[vegeta][job=%s] webhook inbound: status=%s success=%s "
            "partial=%s warnings=%d job_state=%s age_since_dispatch=%.0fs",
            record.name, data.get("status"), data.get("success"),
            data.get("partial"),
            len(data.get("warnings") or []),
            record.state, _age,
        )

        # Lightweight "extraction started" ping — the Lambda sends this when it
        # actually picks the job up. Update last_heartbeat so the watchdog
        # measures real progress, not time-since-state-change (a job merely
        # queued in AWS shouldn't be killed for being slow to start).
        if data.get("status") == "started":
            if record.state == "extracting":
                record.write({"last_heartbeat": _now})
                # age here = AWS async-queue latency: time from Odoo's
                # lambda:Invoke until the Lambda container actually began
                # running. If routinely >5min the batch is outrunning the
                # Lambda's reserved concurrency.
                _logger.info(
                    "[vegeta][job=%s] extraction STARTED ping — "
                    "aws_queue_latency=%.0fs", record.name, _age,
                )
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
                _logger.warning(
                    "[vegeta][job=%s] extraction reported FAILURE by Lambda "
                    "after %.0fs — marking job failed: %s",
                    record.name, _age, str(error_msg)[:300],
                )
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
            signals = data.get("signals")
            is_partial = data.get("partial", False)
            warnings = data.get("warnings") or []

            ICP = request.env["ir.config_parameter"].sudo()
            s3_bucket = ICP.get_param("vegeta.s3_bucket")
            s3_key_id = ICP.get_param("vegeta.s3_access_key_id")
            s3_secret = ICP.get_param("vegeta.s3_secret_access_key")
            s3_region = ICP.get_param("vegeta.s3_region") or "us-east-1"
            s3_folder = ICP.get_param("vegeta.s3_folder") or "vegeta"
            cdn_url = ICP.get_param("vegeta.s3_cdn_url")
            s3_endpoint_url = ICP.get_param("vegeta.s3_endpoint_url") or ""

            artifacts_url = None
            if s3_bucket and (artifacts or screenshot_keys or asset_keys):
                from ..services.s3_service import (
                    upload_artifacts_to_s3,
                    get_artifacts_folder_url,
                )

                if artifacts and not (screenshot_keys or asset_keys):
                    upload_artifacts_to_s3(
                        artifacts=artifacts,
                        job_name=record.name,
                        bucket=s3_bucket,
                        access_key_id=s3_key_id,
                        secret_key=s3_secret,
                        region=s3_region,
                        folder=s3_folder,
                        cdn_url=cdn_url,
                        endpoint_url=s3_endpoint_url,
                    )
                artifacts_url = get_artifacts_folder_url(
                    job_name=record.name,
                    bucket=s3_bucket,
                    region=s3_region,
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
            if signals:
                write_vals["signals_json"] = signals
            # Surface partial/warnings WITHOUT polluting error_message — a
            # successful-but-imperfect extraction is not a red failure. The
            # tasker sees a non-red "Partial extraction" banner instead.
            if warnings or is_partial:
                summary = warnings or ["Extraction was partial (deadline reached)."]
                write_vals["extraction_warnings"] = (
                    "\n".join(f"• {w}" for w in summary)[:1000]
                )
                _logger.info(
                    "[vegeta][job=%s] extraction succeeded with warnings: %s",
                    record.name, summary,
                )
            else:
                write_vals["extraction_warnings"] = False

            # Full transparency: persist the complete Lambda callback. Artifacts
            # are trimmed to filenames — their content is large and already on S3.
            # Size-cap at 256KB to prevent runaway JSONB growth on misbehaving payloads.
            callback_snapshot = dict(data)
            if isinstance(callback_snapshot.get("artifacts"), dict):
                callback_snapshot["artifacts"] = sorted(callback_snapshot["artifacts"].keys())
            _CALLBACK_MAX_BYTES = 256 * 1024
            _serialized = json.dumps(callback_snapshot, default=str)
            if len(_serialized) > _CALLBACK_MAX_BYTES:
                callback_snapshot = {
                    "_truncated": True,
                    "_original_size_bytes": len(_serialized),
                    "_max_size_bytes": _CALLBACK_MAX_BYTES,
                    "keys": sorted(callback_snapshot.keys()),
                }
            write_vals["lambda_callback_json"] = callback_snapshot

            # auto_continue (default True: Run All / batch / retry / rerun) ->
            # cascade straight into PRD generation. When False (staged manual
            # run) the job parks at 'extracted' and waits for the Generate
            # button — the single decision point that severs the
            # extraction->generation auto-link.
            auto = record.auto_continue
            next_state = "generating" if auto else "extracted"
            write_vals["state"] = next_state
            record.write(write_vals)

            # Extraction handoff complete — the boundary between the
            # "extraction" timeline and the "PRD generation" timeline. Grep
            # this to confirm a job left `extracting` cleanly.
            _logger.info(
                "[vegeta][job=%s] extraction COMPLETE after %.0fs "
                "(partial=%s) — prd_prompt=%dB screenshots=%d assets=%d "
                "-> state=%s (auto_continue=%s)",
                record.name, _age, is_partial,
                len(prd_prompt or ""),
                len(screenshot_keys or []), len(asset_keys or []),
                next_state, auto,
            )

            # Notify browser of state change
            try:
                request.env["bus.bus"]._sendone(
                    "vegeta_job_updates",
                    "vegeta/job_state",
                    {"id": record.id, "state": next_state},
                )
            except Exception:
                pass

            # Use postcommit to ensure data is committed before the background
            # thread reads it (fixes race condition LG-3). PRD generation
            # auto-fires ONLY in auto_continue mode; staged jobs stop at
            # 'extracted' and are advanced by the Generate button.
            db_name = request.env.cr.dbname
            record_id = record.id

            def _deferred():
                from ..models.vegeta_job import _submit_bg
                _submit_bg(
                    f"prd-gen[job={record_id}]",
                    record._run_prd_generation_bg, db_name, record_id,
                )

            if auto:
                request.env.cr.postcommit.add(_deferred)

            return Response(
                json.dumps({"status": "success", "next_step": next_state}),
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
        "/api/v1/vegeta/jobs/<int:job_id>/status",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def get_job_status(self, job_id, **kwargs):
        """Job status API."""
        try:
            record = request.env["vegeta.job"].browse(job_id)
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
