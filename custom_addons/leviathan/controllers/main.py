"""Leviathan HTTP controllers — webhook endpoint + API."""

import hmac
import json
import logging
import os

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _verify_webhook_token():
    """Verify the X-Leviathan-Token header against the shared secret.

    If LEVIATHAN_WEBHOOK_TOKEN env var is not set, auth is bypassed with a
    warning.  In production the env var MUST be set — deployment scripts
    enforce this.
    """
    secret = os.environ.get("LEVIATHAN_WEBHOOK_TOKEN")
    if not secret:
        _logger.warning(
            "LEVIATHAN_WEBHOOK_TOKEN not set -- webhook auth DISABLED. "
            "Set this env var in production!"
        )
        return True
    token = request.httprequest.headers.get("X-Leviathan-Token")
    return hmac.compare_digest(token or "", secret)


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

        try:
            data = json.loads(request.httprequest.data)
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

        if record.state != "extracting":
            _logger.info(
                "Webhook for job %s ignored: state is '%s' "
                "(expected 'extracting')",
                job_id,
                record.state,
            )
            return Response(
                json.dumps({
                    "ignored": True,
                    "reason": f"Job in state '{record.state}'",
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

            ICP = request.env["ir.config_parameter"].sudo()
            s3_bucket = ICP.get_param("leviathan.s3_bucket")
            s3_key_id = ICP.get_param("leviathan.s3_access_key_id")
            s3_secret = ICP.get_param("leviathan.s3_secret_access_key")
            s3_region = ICP.get_param("leviathan.s3_region") or "us-east-1"
            s3_folder = ICP.get_param("leviathan.s3_folder") or "leviathan"
            cdn_url = ICP.get_param("leviathan.s3_cdn_url")

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

            write_vals["state"] = "generating"
            record.write(write_vals)

            # Use postcommit to ensure data is committed before
            # background thread reads it (fixes race condition LG-3)
            db_name = request.env.cr.dbname
            record_id = record.id

            def _deferred():
                from ..models.leviathan_job import _POOL
                _POOL.submit(
                    lambda: record._run_prd_generation_bg(db_name, record_id)
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
