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

        try:
            record_id = int(job_id)
        except (ValueError, TypeError):
            return Response("Invalid job_id", status=400)

        build = request.env["kaiju.commit0"].sudo().browse(record_id)
        if not build.exists():
            _logger.warning("Build callback for non-existent record id=%s", job_id)
            return Response("Not found", status=404)

        # Idempotency: if already in terminal state, skip duplicate callback
        if build.build_status in ("done", "failed"):
            _logger.info(
                "Build %s already finalized (status=%s); ignoring duplicate callback",
                build.name, build.build_status,
            )
            return Response(
                json.dumps({"ok": True, "duplicate": True}),
                status=200, content_type="application/json",
            )

        from odoo import fields as odoo_fields

        # CRITICAL: Sync steps + persist logs BEFORE flipping to terminal state.
        # Once build_status='done'/'failed', the cron stops polling this record —
        # if pods get GC'd (5min) before we capture logs, they are gone forever.
        # Run sync/persist eagerly so logs are in Postgres before pod GC fires.
        try:
            build._sync_steps()
        except Exception as e:
            _logger.warning(
                "Callback: failed to sync steps for build %s: %s", build.name, e,
            )
        try:
            build._persist_step_logs_incremental()
        except Exception as e:
            _logger.warning(
                "Callback: failed to persist incremental logs for build %s: %s",
                build.name, e,
            )
        # Belt-and-braces full persist (idempotent: skips steps with has_log=True)
        try:
            build._persist_step_logs()
        except Exception as e:
            _logger.warning(
                "Callback: failed full step-log persist for build %s: %s",
                build.name, e,
            )

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
            # Pull rich S3 metadata once on completion (after image/dataset URIs set)
            try:
                build._fetch_pipeline_metadata()
            except Exception as e:
                _logger.warning(
                    "Callback: failed to fetch pipeline metadata for build %s: %s",
                    build.name, e,
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

        try:
            record_id = int(job_id)
        except (ValueError, TypeError):
            return Response("Invalid job_id", status=400)

        run = request.env["kaiju.commit0.run"].sudo().browse(record_id)
        if not run.exists():
            _logger.warning("Run callback for non-existent record id=%s", job_id)
            return Response("Not found", status=404)

        # Idempotency: skip duplicate callback if already finalized
        if run.run_status in ("done", "failed"):
            _logger.info(
                "Run %s already finalized (status=%s); ignoring duplicate callback",
                run.name, run.run_status,
            )
            return Response(
                json.dumps({"ok": True, "duplicate": True}),
                status=200, content_type="application/json",
            )

        from odoo import fields as odoo_fields

        # CRITICAL: capture per-step logs BEFORE flipping to terminal status,
        # otherwise the cron stops polling and pod GC (5min) destroys the source.
        try:
            run._sync_steps()
        except Exception as e:
            _logger.warning(
                "Callback: failed to sync steps for run %s: %s", run.name, e,
            )
        try:
            run._persist_step_logs_incremental()
        except Exception as e:
            _logger.warning(
                "Callback: failed to persist incremental logs for run %s: %s",
                run.name, e,
            )
        try:
            run._persist_step_logs()
        except Exception as e:
            _logger.warning(
                "Callback: failed full step-log persist for run %s: %s",
                run.name, e,
            )

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
