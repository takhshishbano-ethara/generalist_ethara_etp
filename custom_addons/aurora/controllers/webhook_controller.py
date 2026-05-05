from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


_WEBHOOK_MAX_SKEW_SECONDS = 300


def _legacy_auth_enabled(env) -> bool:
    val = env["ir.config_parameter"].sudo().get_param(
        "aurora.webhook_legacy_auth_enabled", "True",
    )
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _raw_body() -> bytes:
    try:
        return request.httprequest.get_data(cache=True) or b""
    except Exception:
        return b""


def _verify_hmac(secret: str) -> bool:
    if not secret:
        return False
    headers = request.httprequest.headers
    ts = headers.get("X-Aurora-Timestamp", "")
    sig = headers.get("X-Aurora-Signature", "")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    now = int(time.time())
    if abs(now - ts_int) > _WEBHOOK_MAX_SKEW_SECONDS:
        _logger.warning("Aurora webhook: stale timestamp (skew=%ds)", now - ts_int)
        return False
    mac = hmac.new(
        secret.encode("utf-8"),
        msg=f"{ts}.".encode("utf-8") + _raw_body(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(mac, sig)


def _verify_legacy_token(secret: str) -> bool:
    if not secret:
        return False
    received = request.httprequest.headers.get("X-Aurora-Token", "")
    return bool(received) and hmac.compare_digest(received, secret)


def _verify_token() -> bool:
    secret = os.environ.get("AURORA_WEBHOOK_SECRET", "").strip()
    if not secret:
        _logger.warning("Aurora webhook: AURORA_WEBHOOK_SECRET env var is not set; rejecting request.")
        return False
    if _verify_hmac(secret):
        return True
    if _legacy_auth_enabled(request.env) and _verify_legacy_token(secret):
        _logger.info("Aurora webhook: accepted via legacy X-Aurora-Token path (flag is on).")
        return True
    return False


_ALLOWED_PIPELINE_FIELDS = frozenset({
    "stage",
    "step1_status", "step2_status", "step3_status",
    "step4_status", "step5_status", "step6_status",
    "phase1_status", "phase2_status", "phase3_status",
    "progress_text",
    "pr_count", "filtered_pr_count", "tag_count",
    "group_count", "issue_count", "dataset_count",
    "phase2_dataset_count", "phase2_image_count",
    "phase2_instance_count", "phase2_resolved_count",
    "phase3_inference_count", "phase3_pass_at_k",
    "detected_lang",
    "dataset_url", "dataset_filename",
    "phase1_file", "phase2_file", "phase2_dataset_file",
    "phase2_final_report_file", "phase3_file",
    "step1_file", "step2_file", "step3_file",
    "step4_file", "step5_file", "step6_file",
    "phase2_has_registry",
})

_ALLOWED_EVALUATION_FIELDS = frozenset({
    "stage", "build_status", "run_status", "report_status",
    "progress_text",
    "total_instances", "resolved_instances",
    "unresolved_instances", "error_instances",
    "final_report_file", "missing_registries",
    "s3_run_number", "patch_file", "repo_dir", "workdir",
})

_ALLOWED_STAGING_FIELDS = frozenset({
    "stage", "test_result", "staging_path",
})


def _filter_payload(payload: dict, allowed: frozenset) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in allowed}


def _append_log(record, message: str) -> None:
    if not message:
        return
    current = record.log or ""
    lines = (current + "\n" + message).splitlines()
    if len(lines) > 5000:
        lines = lines[-4000:]
    record.sudo().write({"log": "\n".join(lines)})


class AuroraWebhookController(http.Controller):

    @http.route(
        "/aurora/webhook/pipeline",
        type="jsonrpc", auth="none", csrf=False, methods=["POST"],
    )
    def pipeline_webhook(self, **kwargs):
        if not _verify_token():
            return {"error": "unauthorized"}

        pipeline_id = kwargs.get("pipeline_id")
        if not pipeline_id:
            return {"error": "missing pipeline_id"}

        rec = request.env["aurora.pipeline"].sudo().browse(int(pipeline_id))
        if not rec.exists():
            return {"error": "pipeline not found"}

        values = _filter_payload(kwargs, _ALLOWED_PIPELINE_FIELDS)
        values["last_heartbeat"] = fields.Datetime.now()

        try:
            rec.write(values)
        except Exception:
            _logger.exception("Aurora webhook: pipeline write failed for id=%s", pipeline_id)
            return {"error": "write failed"}

        message = kwargs.get("message")
        if message:
            _append_log(rec, str(message))

        return {"ok": True}

    @http.route(
        "/aurora/webhook/evaluation",
        type="jsonrpc", auth="none", csrf=False, methods=["POST"],
    )
    def evaluation_webhook(self, **kwargs):
        if not _verify_token():
            return {"error": "unauthorized"}

        evaluation_id = kwargs.get("evaluation_id")
        if not evaluation_id:
            return {"error": "missing evaluation_id"}

        rec = request.env["aurora.evaluation"].sudo().browse(int(evaluation_id))
        if not rec.exists():
            return {"error": "evaluation not found"}

        values = _filter_payload(kwargs, _ALLOWED_EVALUATION_FIELDS)
        values["last_heartbeat"] = fields.Datetime.now()

        try:
            rec.write(values)
        except Exception:
            _logger.exception("Aurora webhook: evaluation write failed for id=%s", evaluation_id)
            return {"error": "write failed"}

        message = kwargs.get("message")
        if message:
            _append_log(rec, str(message))

        return {"ok": True}

    @http.route(
        "/aurora/webhook/harness_staging",
        type="jsonrpc", auth="none", csrf=False, methods=["POST"],
    )
    def harness_staging_webhook(self, **kwargs):
        if not _verify_token():
            return {"error": "unauthorized"}

        staging_id = kwargs.get("staging_id")
        if not staging_id:
            return {"error": "missing staging_id"}

        rec = request.env["aurora.harness.staging"].sudo().browse(int(staging_id))
        if not rec.exists():
            return {"error": "staging not found"}

        values = _filter_payload(kwargs, _ALLOWED_STAGING_FIELDS)

        try:
            rec.write(values)
        except Exception:
            _logger.exception("Aurora webhook: staging write failed for id=%s", staging_id)
            return {"error": "write failed"}

        message = kwargs.get("message")
        if message:
            current = rec.test_log or ""
            lines = (current + "\n" + str(message)).splitlines()
            if len(lines) > 5000:
                lines = lines[-4000:]
            rec.sudo().write({"test_log": "\n".join(lines)})

        return {"ok": True}
