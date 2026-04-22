# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging

import httpx

from ..services.rabbitmq_service import batch_publish_eval_tasks, publish_eval_task

_logger = logging.getLogger(__name__)

_HTTP_CLIENT = httpx.Client(
    http2=True,
    follow_redirects=True,
    timeout=httpx.Timeout(None, connect=10.0),
)


class BerserkerController(http.Controller):
    @http.route(
        "/api/berserker/get_jsonl_data",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def method_get_jsonl_data(self, **params):
        try:
            # Parse request body
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except Exception:
                try:
                    jdata = json.loads(request.httprequest.data)
                except Exception:
                    jdata = {}

            if "url" not in jdata:
                return http.Response(
                    json.dumps({"message": "URL not in body", "status": 400}),
                    content_type="application/json",
                    status=400,
                )
            if not jdata["url"]:
                return http.Response(
                    json.dumps({"message": "URL is empty", "status": 400}),
                    content_type="application/json",
                    status=400,
                )

            url = str(jdata["url"])

            # Fetch JSONL content from the URL
            response = _HTTP_CLIENT.get(url, timeout=60)
            response.raise_for_status()

            # Parse concatenated JSON objects (multi-line or single-line)
            data = []
            text = response.text.strip()
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                while idx < len(text) and text[idx] in " \t\n\r":
                    idx += 1
                if idx >= len(text):
                    break
                obj, end_idx = decoder.raw_decode(text, idx)
                data.append(obj)
                idx = end_idx

            if not data:
                return http.Response(
                    json.dumps({"message": "Data Not Found", "status": 400}),
                    content_type="application/json",
                    status=400,
                )

            # Map each parsed object to berserker record vals
            vals_list = []
            for item in data:
                vals = {
                    "task_id": item.get("evaluation_id", ""),
                    "client_prompt": item.get("prompt", ""),
                }
                vals_list.append(vals)

            # Create records in chunks of 100, committing after each chunk
            CREATE_CHUNK = 100
            all_record_ids = []

            for chunk_start in range(0, len(vals_list), CREATE_CHUNK):
                chunk_vals = vals_list[chunk_start : chunk_start + CREATE_CHUNK]
                chunk_records = request.env["berserker"].sudo().create(chunk_vals)
                all_record_ids.extend(chunk_records.ids)
                request.env.cr.commit()

                _logger.info(
                    "Processed chunk %d-%d / %d records",
                    chunk_start + 1,
                    chunk_start + len(chunk_vals),
                    len(vals_list),
                )

            # Mark all records as evaluating BEFORE publishing (avoid race condition)
            if all_record_ids:
                request.env["berserker"].sudo().browse(all_record_ids).write(
                    {"eval_status": "evaluating"}
                )
                request.env.cr.commit()

            # Publish to RabbitMQ for external consumer processing
            queued_count = 0
            queue_errors = []
            if all_record_ids:
                try:
                    batch_publish_eval_tasks(all_record_ids)
                    queued_count = len(all_record_ids)
                except Exception as eq:
                    _logger.error(
                        "Batch publish failed, falling back to individual: %s", eq
                    )
                    for rid in all_record_ids:
                        try:
                            publish_eval_task(rid)
                            queued_count += 1
                        except Exception as eq2:
                            _logger.error(
                                "Failed to queue eval for record %s: %s", rid, eq2
                            )
                            queue_errors.append({"record_id": rid, "error": str(eq2)})

                _logger.info(
                    "Queued %d/%d eval jobs to RabbitMQ",
                    queued_count,
                    len(all_record_ids),
                )

            return http.Response(
                json.dumps(
                    {
                        "success": True,
                        "message": "Success",
                        "records_created": len(all_record_ids),
                        "records_queued": queued_count,
                        "queue_errors": queue_errors,
                        "status": 200,
                    }
                ),
                content_type="application/json",
                status=200,
            )

        except Exception as e:
            _logger.exception("Error in get_jsonl_data")
            return http.Response(
                json.dumps({"error": str(e), "status": 500}),
                content_type="application/json",
                status=500,
            )
