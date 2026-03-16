# -*- coding: utf-8 -*-
"""
Evaluation & Processing API endpoints.

POST /api/v1/tasks/<id>/eval            — Trigger full eval_task() pipeline
POST /api/v1/tasks/<id>/evaluate-scores — Trigger evaluate_task() (error checking)
POST /api/v1/tasks/<id>/submit-prompt   — Re-eval with enhanced prompt
POST /api/v1/tasks/<id>/qc              — Trigger QC checks
GET  /api/v1/tasks/<id>/eval-status     — Poll eval/QC processing status
POST /api/v1/ingest/jsonl               — Import tasks from JSONL URL
POST /api/v1/ingest/llm-responses       — Trigger LLM response generation
POST /api/v1/tasks/bulk-eval            — Queue batch eval via RabbitMQ
"""
import json
import logging
import random

import requests as py_requests

from odoo import http
from odoo.http import request

from .helpers import (
    json_error,
    json_success,
    jwt_required,
    parse_json_body,
)

_logger = logging.getLogger(__name__)


class EvalController(http.Controller):
    """REST endpoints for evaluation pipeline operations."""

    # ------------------------------------------------------------------
    # E1: Trigger full eval_task
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/eval",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def trigger_eval(self, task_id, **kw):
        """Trigger the full eval_task() pipeline for a single record.

        This runs synchronously (typically ~60s). For batch operations,
        use /api/v1/tasks/bulk-eval which queues via RabbitMQ.
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return json_error("Task not found", 404)

        try:
            record.eval_task()
            return json_success(
                data={
                    "is_processed": bool(record.is_processed),
                    "is_ratable": bool(record.is_ratable),
                    "is_eval_done": bool(record.is_eval_done),
                },
                message="Evaluation completed",
            )
        except Exception as e:
            _logger.error("eval_task failed for record %s: %s", task_id, e)
            return json_error(f"Evaluation failed: {e}", 500)

    # ------------------------------------------------------------------
    # E2: Trigger evaluate_task (error checking only)
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/evaluate-scores",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def evaluate_scores(self, task_id, **kw):
        """Run evaluate_task() to compare human vs LLM scores and flag errors."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return json_error("Task not found", 404)

        try:
            record.evaluate_task()
            # Count errors
            error_count = 0
            for field_name in record._fields:
                if field_name.startswith("error_") and getattr(record, field_name, False):
                    error_count += 1

            return json_success(
                data={
                    "is_eval_done": bool(record.is_eval_done),
                    "errors_found": error_count,
                },
                message="Score evaluation completed",
            )
        except Exception as e:
            _logger.error("evaluate_task failed for record %s: %s", task_id, e)
            return json_error(f"Score evaluation failed: {e}", 500)

    # ------------------------------------------------------------------
    # E3: Submit prompt (re-eval with enhanced prompt)
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/submit-prompt",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def submit_prompt(self, task_id, **kw):
        """Trigger action_submit_prompt() — clears downstream fields and re-runs eval."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return json_error("Task not found", 404)

        # Optionally update enhance_prompt from body before triggering
        body = parse_json_body()
        if body.get("enhance_prompt"):
            record.write({"enhance_prompt": body["enhance_prompt"]})

        try:
            record.action_submit_prompt()
            return json_success(
                data={
                    "is_processed": bool(record.is_processed),
                    "is_eval_done": bool(record.is_eval_done),
                },
                message="Enhanced prompt submitted and re-evaluated",
            )
        except Exception as e:
            _logger.error("action_submit_prompt failed for record %s: %s", task_id, e)
            return json_error(f"Re-evaluation failed: {e}", 500)

    # ------------------------------------------------------------------
    # E4: Trigger QC checks
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/qc",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["quality_lead", "admin"])
    def trigger_qc(self, task_id, **kw):
        """Trigger run_qc_checks() for a single record."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return json_error("Task not found", 404)

        try:
            record.run_qc_checks()
            return json_success(
                data={
                    "qc_task_status": record.qc_task_status or None,
                    "qc_score": record.qc_score or 0,
                },
                message="QC checks completed",
            )
        except Exception as e:
            _logger.error("run_qc_checks failed for record %s: %s", task_id, e)
            return json_error(f"QC checks failed: {e}", 500)

    # ------------------------------------------------------------------
    # E5: Poll eval status
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/<int:task_id>/eval-status",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["tasker", "quality_lead", "admin"])
    def eval_status(self, task_id, **kw):
        """Poll the processing status of a task."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        env = request.jwt_env
        record = env["preference.ranking"].sudo().browse(task_id)
        if not record.exists():
            return json_error("Task not found", 404)

        # Determine completed stages by checking which store fields have data
        stages_completed = []
        stages_pending = []

        # Check responses
        if record.gpt_response or record.gemini_response:
            stages_completed.append("responses")
        else:
            stages_pending.append("responses")

        # Check eval sections
        if record.store_truthfulness_a:
            stages_completed.append("eval_ab")
        else:
            stages_pending.append("eval_ab")

        if record.store_ophelia_truthfulness_a:
            stages_completed.append("eval_oph")
        else:
            stages_pending.append("eval_oph")

        if record.store_gpt_truthfulness_a:
            stages_completed.append("eval_gpt_sxs")
        else:
            stages_pending.append("eval_gpt_sxs")

        if record.store_gemini_truthfulness_b:
            stages_completed.append("eval_gem_sxs")
        else:
            stages_pending.append("eval_gem_sxs")

        if record.store_rubric1_name:
            stages_completed.append("rubrics")
        else:
            stages_pending.append("rubrics")

        if record.qc_task_status:
            stages_completed.append("qc")
        else:
            stages_pending.append("qc")

        return json_success(data={
            "is_processed": bool(record.is_processed),
            "is_ratable": bool(record.is_ratable),
            "is_eval_done": bool(record.is_eval_done),
            "qc_task_status": record.qc_task_status or None,
            "stages_completed": stages_completed,
            "stages_pending": stages_pending,
        })

    # ------------------------------------------------------------------
    # B1: JSONL Import
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/ingest/jsonl",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["admin"])
    def ingest_jsonl(self, **kw):
        """Import tasks from a JSONL URL.

        Body: {"url": "https://..."}

        This is the API version of the existing /api/get_jsonl_data endpoint.
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        body = parse_json_body()
        url = body.get("url", "").strip()

        if not url:
            return json_error("url is required", 400)

        try:
            response = py_requests.get(url, timeout=60)
            response.raise_for_status()
        except Exception as e:
            return json_error(f"Failed to fetch URL: {e}", 400)

        # Parse concatenated JSON objects
        data = []
        text = response.text.strip()
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(text):
            while idx < len(text) and text[idx] in " \t\n\r":
                idx += 1
            if idx >= len(text):
                break
            try:
                obj, end_idx = decoder.raw_decode(text, idx)
                data.append(obj)
                idx = end_idx
            except json.JSONDecodeError:
                break

        if not data:
            return json_error("No data found at URL", 400)

        # Randomize response order for half the records
        flags = [True] * (len(data) // 2) + [False] * (len(data) - len(data) // 2)
        random.shuffle(flags)
        for d, swap in zip(data, flags):
            if swap:
                d["response_a"], d["response_b"] = d["response_b"], d["response_a"]
            d["is_randomized"] = swap

        env = request.jwt_env
        vals_list = []
        for i in data:
            prompt_text = ""
            prompt_metadata = i.get("prompt_metadata", {})
            dialog_history = prompt_metadata.get("dialog_history", [])
            if dialog_history:
                prompt_text = dialog_history[-1].get("content", "")
            vals_list.append({
                "task_id": i.get("evaluation_id", "") or "",
                "client_prompt": prompt_text,
                "client_response_a": i.get("response_a", "") or "",
                "client_response_b": i.get("response_b", "") or "",
                "is_randomized": i.get("is_randomized", False),
            })

        from ...services.rabbitmq_service import batch_publish_eval_tasks

        CREATE_CHUNK = 100
        all_record_ids = []
        queued_count = 0
        queue_errors = []

        for chunk_start in range(0, len(vals_list), CREATE_CHUNK):
            chunk_vals = vals_list[chunk_start:chunk_start + CREATE_CHUNK]
            chunk_records = env["preference.ranking"].sudo().create(chunk_vals)
            chunk_ids = chunk_records.ids
            all_record_ids.extend(chunk_ids)
            env.cr.commit()

            try:
                batch_publish_eval_tasks(chunk_ids)
                queued_count += len(chunk_ids)
            except Exception as eq:
                _logger.error("Batch publish failed: %s", eq)
                for rid in chunk_ids:
                    try:
                        from ...services.rabbitmq_service import publish_eval_task
                        publish_eval_task(rid)
                        queued_count += 1
                    except Exception as eq2:
                        queue_errors.append({"record_id": rid, "error": str(eq2)})

        return json_success(
            data={
                "records_created": len(all_record_ids),
                "records_queued": queued_count,
                "queue_errors": queue_errors,
            },
            message=f"Imported {len(all_record_ids)} tasks",
            status=201,
        )

    # ------------------------------------------------------------------
    # B4: Bulk eval via RabbitMQ
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/tasks/bulk-eval",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required(roles=["admin"])
    def bulk_eval(self, **kw):
        """Queue eval tasks for a list of record IDs via RabbitMQ.

        Body: {"record_ids": [1, 2, 3, ...]}
              OR {"filter": {"is_processed": false}} to queue matching records
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        body = parse_json_body()
        env = request.jwt_env
        record_ids = body.get("record_ids", [])

        # Allow filter-based queuing
        if not record_ids and body.get("filter"):
            domain = []
            f = body["filter"]
            if "is_processed" in f:
                domain.append(("is_processed", "=", bool(f["is_processed"])))
            if "is_ratable" in f:
                domain.append(("is_ratable", "=", bool(f["is_ratable"])))
            records = env["preference.ranking"].sudo().search(domain, limit=5000)
            record_ids = records.ids

        if not record_ids:
            return json_error("No records to process", 400)

        from ...services.rabbitmq_service import batch_publish_eval_tasks

        try:
            batch_publish_eval_tasks(record_ids)
            return json_success(
                data={"queued": len(record_ids)},
                message=f"Queued {len(record_ids)} tasks for evaluation",
            )
        except Exception as e:
            _logger.error("Bulk eval queue failed: %s", e)
            return json_error(f"Queue failed: {e}", 500)
