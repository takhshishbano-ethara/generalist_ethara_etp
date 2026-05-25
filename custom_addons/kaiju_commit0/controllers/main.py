# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

WEBHOOK_TOKEN_PARAM = "kaiju.webhook_token"


class KaijuCallbackController(http.Controller):
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

    def _upsert_callback_steps(
        self, parent_record, steps_data, parent_field, workflow_status=None
    ):
        if not steps_data or not isinstance(steps_data, list):
            return
        Step = request.env["kaiju.commit0.workflow.step"].sudo()
        existing_map = {
            s.node_id: s for s in Step.search([(parent_field, "=", parent_record.id)])
        }
        phase_map = {
            "Succeeded": "Succeeded",
            "Failed": "Failed",
            "Error": "Error",
            "Running": "Running",
            "Skipped": "Skipped",
            "Omitted": "Omitted",
        }
        upserted = 0
        for step_data in steps_data:
            if not isinstance(step_data, dict):
                continue
            name = step_data.get("name") or ""
            order = step_data.get("order", 0)
            log_file = step_data.get("log_file") or ""
            if not name or not log_file:
                continue
            node_id = f"callback-{order}"
            phase_raw = (step_data.get("phase") or "Pending").capitalize()
            phase = phase_map.get(phase_raw, "Pending")
            vals = {
                "step_name": name,
                "phase": phase,
                "log_file": step_data.get("log_file", ""),
                "step_order": order,
                "node_type": "Pod",
            }
            if node_id in existing_map:
                existing_map[node_id].write(vals)
            else:
                vals["node_id"] = node_id
                vals[parent_field] = parent_record.id
                Step.create(vals)
            upserted += 1
        _logger.info(
            "Upserted %d callback steps for %s (id=%s, %d raw in payload)",
            upserted,
            parent_record._name,
            parent_record.id,
            len(steps_data),
        )

        # Phase inference: when the Argo API was unavailable in the exit
        # hook, all phases arrive as "Unknown" which maps to "Pending".
        # Infer correct phases from the overall workflow outcome.
        if workflow_status and upserted:
            pending = Step.search(
                [
                    (parent_field, "=", parent_record.id),
                    ("phase", "=", "Pending"),
                ],
                order="step_order asc",
            )
            if pending:
                if workflow_status == "success":
                    pending.write({"phase": "Succeeded"})
                else:
                    # Failed workflow: earlier steps succeeded, last failed
                    if len(pending) > 1:
                        pending[:-1].write({"phase": "Succeeded"})
                    pending[-1:].write({"phase": "Failed"})
                _logger.info(
                    "Inferred phase for %d step(s) from workflow_status=%s "
                    "on %s (id=%s)",
                    len(pending),
                    workflow_status,
                    parent_record._name,
                    parent_record.id,
                )

    @http.route(
        "/kaiju/callback/build", type="http", auth="none", methods=["POST"], csrf=False
    )
    def callback_build(self, **kwargs):
        """Receive build pipeline completion callback.

        Expected JSON payload::

            {
                "job_id": "<odoo record id>",
                "status": "success" | "failed",
                "image_uri": "<ECR image URI>",
                "s3_dataset_uri": "<S3 path to dataset_entries.json>",
                "s3_log_prefix": "kaiju_logs/<RepoFlat>/<job_id>/",
                "steps": [
                    {"name": "clone-repo", "log_file": "clone-repo.log",
                     "phase": "Succeeded", "order": 1},
                    ...
                ]
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
                build.name,
                build.build_status,
            )
            return Response(
                json.dumps({"ok": True, "duplicate": True}),
                status=200,
                content_type="application/json",
            )

        from odoo import fields as odoo_fields

        # Prefer s3_log_uri (full s3://bucket/prefix/) over s3_log_prefix (key-only)
        s3_log_prefix = data.get("s3_log_uri") or data.get("s3_log_prefix") or ""

        if status == "success":
            build.write(
                {
                    "build_status": "done",
                    "build_end": odoo_fields.Datetime.now(),
                    "image_uri": data.get("image_uri", ""),
                    "s3_dataset_uri": data.get("s3_dataset_uri", ""),
                    "s3_log_prefix": s3_log_prefix,
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
                    "s3_log_prefix": s3_log_prefix,
                    "build_log": self._append_log(build.build_log, f"✗ {message}"),
                }
            )

        self._upsert_callback_steps(build, data.get("steps", []), "build_id", status)

        _logger.info("Build callback processed: build=%s status=%s", build.name, status)
        return Response(
            json.dumps({"ok": True}), status=200, content_type="application/json"
        )

    @http.route(
        "/kaiju/callback/run", type="http", auth="none", methods=["POST"], csrf=False
    )
    def callback_run(self, **kwargs):
        """Receive run pipeline completion callback.

        Expected JSON payload::

            {
                "job_id": "<odoo record id>",
                "status": "success" | "failed",
                "s3_log_prefix": "kaiju_logs/<RepoFlat>/<job_id>/",
                "steps": [{"name": "...", "log_file": "...", "phase": "...", "order": N}, ...],
                "pass_rate": 85.0,
                "tests_passed": 12, "tests_failed": 2, "tests_total": 14,
                "duration_seconds": 342.5, "cost_usd": 1.23,
                "tokens_input": 150000, "tokens_output": 45000
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
                run.name,
                run.run_status,
            )
            return Response(
                json.dumps({"ok": True, "duplicate": True}),
                status=200,
                content_type="application/json",
            )

        from odoo import fields as odoo_fields

        s3_log_prefix = data.get("s3_log_uri") or data.get("s3_log_prefix") or ""

        if status == "success":
            run.write(
                {
                    "run_status": "done",
                    "run_end": odoo_fields.Datetime.now(),
                    "s3_log_prefix": s3_log_prefix,
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
                    "s3_log_prefix": s3_log_prefix,
                    "run_log": self._append_log(run.run_log, f"✗ {message}"),
                }
            )

        self._upsert_callback_steps(run, data.get("steps", []), "run_id", status)

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
