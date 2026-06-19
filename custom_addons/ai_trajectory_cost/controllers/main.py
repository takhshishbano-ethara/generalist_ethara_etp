import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


TRAJECTORY_COST_FIELDS = {
    "project_key",
    "trajectory_id",
}

MODEL_USAGE_FIELDS = {
    "model_name",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "cost",
}

MODEL_USAGE_KEYS = ("model_usage_ids", "model_usages", "models")


class TrajectoryCostController(http.Controller):
    @http.route(
        "/api/v1/trajectory_cost",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def create_trajectory_cost(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        try:
            payload = self._read_payload()
        except ValueError as e:
            return self._json_error(str(e), status=400)

        try:
            vals = self._filter_fields(payload, TRAJECTORY_COST_FIELDS)

            usages = self._extract_usages(payload)
            if usages is None:
                return self._json_error(
                    "model_usage_ids/model_usages must be a list of objects.",
                    status=400,
                )

            usage_commands = []
            for u in usages:
                if not isinstance(u, dict):
                    continue
                usage_vals = self._filter_fields(u, MODEL_USAGE_FIELDS)
                if usage_vals:
                    usage_commands.append((0, 0, usage_vals))

            if usage_commands:
                vals["model_usage_ids"] = usage_commands

            record = request.env["trajectory.cost"].sudo().create(vals)

            return self._json_response(self._serialize_cost(record))

        except Exception as e:
            _logger.exception("Trajectory cost create error")
            return self._json_error(str(e), status=500)

    @http.route(
        "/api/v1/trajectory_cost/model_usage",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def create_model_usage(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        try:
            payload = self._read_payload()
        except ValueError as e:
            return self._json_error(str(e), status=400)

        try:
            vals = self._filter_fields(payload, MODEL_USAGE_FIELDS)

            trajectory_cost_id = payload.get("trajectory_cost_id")
            if trajectory_cost_id:
                vals["trajectory_cost_id"] = trajectory_cost_id
            else:
                trajectory_id = payload.get("trajectory_id")
                project_key = payload.get("project_key")
                if trajectory_id:
                    domain = [("trajectory_id", "=", trajectory_id)]
                    if project_key:
                        domain.append(("project_key", "=", project_key))
                    parent = (
                        request.env["trajectory.cost"]
                        .sudo()
                        .search(domain, limit=1)
                    )
                    if not parent:
                        parent_vals = {"trajectory_id": trajectory_id}
                        if project_key:
                            parent_vals["project_key"] = project_key
                        parent = (
                            request.env["trajectory.cost"]
                            .sudo()
                            .create(parent_vals)
                        )
                    vals["trajectory_cost_id"] = parent.id

            usage = (
                request.env["trajectory.cost.model.usage"].sudo().create(vals)
            )

            return self._json_response(self._serialize_usage(usage))

        except Exception as e:
            _logger.exception("Trajectory cost model usage create error")
            return self._json_error(str(e), status=500)

    @staticmethod
    def _read_payload():
        raw = request.httprequest.get_data(as_text=True) or ""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON payload: {e}")
        if not isinstance(data, dict):
            raise ValueError("Payload must be a JSON object.")
        return data

    @staticmethod
    def _filter_fields(payload, allowed):
        return {k: payload[k] for k in allowed if k in payload}

    @staticmethod
    def _extract_usages(payload):
        for key in MODEL_USAGE_KEYS:
            if key in payload:
                value = payload[key]
                if value is None:
                    return []
                if not isinstance(value, list):
                    return None
                return value
        return []

    @classmethod
    def _serialize_cost(cls, record):
        return {
            "success": True,
            "id": record.id,
            "project_key": record.project_key,
            "trajectory_id": record.trajectory_id,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cache_tokens": record.cache_tokens,
            "total_tokens": record.total_tokens,
            "cost": record.cost,
            "model_count": record.model_count,
            "model_usage_ids": [
                cls._serialize_usage(u, include_success=False)
                for u in record.model_usage_ids
            ],
        }

    @staticmethod
    def _serialize_usage(record, include_success=True):
        data = {
            "id": record.id,
            "trajectory_cost_id": record.trajectory_cost_id.id,
            "trajectory_id": record.trajectory_id,
            "project_key": record.project_key,
            "model_name": record.model_name,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cache_tokens": record.cache_tokens,
            "total_tokens": record.total_tokens,
            "cost": record.cost,
        }
        if include_success:
            data = {"success": True, **data}
        return data

    @staticmethod
    def _json_response(data, status=200):
        return http.Response(
            json.dumps(data, indent=2, default=str),
            content_type="application/json",
            status=status,
        )

    @staticmethod
    def _json_error(message, status=400):
        return http.Response(
            json.dumps({"success": False, "error": message}),
            content_type="application/json",
            status=status,
        )
