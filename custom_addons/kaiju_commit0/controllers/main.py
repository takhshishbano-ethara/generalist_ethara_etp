# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

WEBHOOK_TOKEN_PARAM = "kaiju.webhook_token"


class KaijuCallbackController(http.Controller):
    """Receives pipeline completion callbacks from Argo workflows.

    Auth: Bearer token validated against ir.config_parameter 'kaiju.webhook_token'.
    Both endpoints are JSON-RPC free (type='json' not used — raw HTTP POST).
    """

    def _validate_token(self):
        auth_header = request.httprequest.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        expected = (
            request.env["ir.config_parameter"].sudo().get_param(WEBHOOK_TOKEN_PARAM, "")
        )
        if not expected:
            _logger.warning("kaiju.webhook_token not configured — rejecting callback")
            return False
        return token == expected

    @http.route(
        "/kaiju/callback/build", type="http", auth="none", methods=["POST"], csrf=False
    )
    def callback_build(self, **kwargs):
        """Receive build pipeline completion callback.

        Expected JSON payload:
            {
                "job_id": "<odoo record id>",
                "status": "success" | "failed",
                "image_uri": "<ECR image URI>",
                "s3_dataset_uri": "<S3 path to dataset_entries.json>"
            }
        """
        if not self._validate_token():
            return Response("Unauthorized", status=401)

        try:
            data = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, TypeError):
            return Response("Invalid JSON", status=400)

        job_id = data.get("job_id")
        status = data.get("status")

        if not job_id or not status:
            return Response("Missing job_id or status", status=400)

        build = request.env["kaiju.commit0"].sudo().browse(int(job_id))
        if not build.exists():
            _logger.warning("Build callback for non-existent record id=%s", job_id)
            return Response("Not found", status=404)

        from odoo import fields as odoo_fields

        if status == "success":
            build.write(
                {
                    "build_status": "done",
                    "build_end": odoo_fields.Datetime.now(),
                    "image_uri": data.get("image_uri", ""),
                    "s3_dataset_uri": data.get("s3_dataset_uri", ""),
                    "build_log": self._append_log(
                        build.build_log, "✓ Build pipeline completed successfully."
                    ),
                }
            )
        else:
            message = data.get("message", "Pipeline reported failure")
            build.write(
                {
                    "build_status": "failed",
                    "build_end": odoo_fields.Datetime.now(),
                    "build_log": self._append_log(build.build_log, f"✗ {message}"),
                }
            )

        _logger.info("Build callback processed: build=%s status=%s", build.name, status)
        return Response(
            json.dumps({"ok": True}), status=200, content_type="application/json"
        )

    @http.route(
        "/kaiju/callback/run", type="http", auth="none", methods=["POST"], csrf=False
    )
    def callback_run(self, **kwargs):
        """Receive run pipeline completion callback.

        Expected JSON payload:
            {
                "job_id": "<odoo record id>",
                "status": "success" | "failed",
                "pass_rate": 85.0,
                "tests_passed": 12,
                "tests_failed": 2,
                "tests_total": 14,
                "duration_seconds": 342.5,
                "cost_usd": 1.23,
                "tokens_input": 150000,
                "tokens_output": 45000,
                "results_s3_url": "...",
                "hf_dataset_url": "...",
                "github_branch_url": "..."
            }
        """
        if not self._validate_token():
            return Response("Unauthorized", status=401)

        try:
            data = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, TypeError):
            return Response("Invalid JSON", status=400)

        job_id = data.get("job_id")
        status = data.get("status")

        if not job_id or not status:
            return Response("Missing job_id or status", status=400)

        run = request.env["kaiju.commit0.run"].sudo().browse(int(job_id))
        if not run.exists():
            _logger.warning("Run callback for non-existent record id=%s", job_id)
            return Response("Not found", status=404)

        from odoo import fields as odoo_fields

        if status == "success":
            run.write(
                {
                    "run_status": "done",
                    "run_end": odoo_fields.Datetime.now(),
                    "pass_rate": data.get("pass_rate", 0.0),
                    "tests_passed": data.get("tests_passed", 0),
                    "tests_failed": data.get("tests_failed", 0),
                    "tests_total": data.get("tests_total", 0),
                    "duration_seconds": data.get("duration_seconds", 0.0),
                    "cost_usd": data.get("cost_usd", 0.0),
                    "tokens_input": data.get("tokens_input", 0),
                    "tokens_output": data.get("tokens_output", 0),
                    "run_log": self._append_log(
                        run.run_log, "✓ Run pipeline completed successfully."
                    ),
                }
            )
        else:
            message = data.get("message", "Pipeline reported failure")
            run.write(
                {
                    "run_status": "failed",
                    "run_end": odoo_fields.Datetime.now(),
                    "run_log": self._append_log(run.run_log, f"✗ {message}"),
                }
            )

        _logger.info("Run callback processed: run=%s status=%s", run.name, status)
        return Response(
            json.dumps({"ok": True}), status=200, content_type="application/json"
        )

    @staticmethod
    def _append_log(existing_log, new_line):
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {new_line}"
        if existing_log:
            return f"{existing_log}\n{entry}"
        return entry
