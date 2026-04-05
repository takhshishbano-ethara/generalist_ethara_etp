# -*- coding: utf-8 -*-
import json as json_lib
import logging
import os

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("KAIJU_WEBHOOK_TOKEN", "")


class KaijuController(http.Controller):
    def _verify_token(self):
        token = request.httprequest.headers.get("X-Kaiju-Token", "")
        if not WEBHOOK_SECRET or token != WEBHOOK_SECRET:
            return False
        return True

    # ── Webhook endpoints (auth=none, token-verified) ───────────────────

    @http.route(
        "/kaiju/webhook", type="jsonrpc", auth="none", methods=["POST"], csrf=False
    )
    def build_webhook(self, **kwargs):
        if not self._verify_token():
            return {"error": "unauthorized"}

        build_id = kwargs.get("build_id")
        if not build_id:
            return {"error": "missing build_id"}

        build = (
            request.env["kaiju.build"]
            .sudo()
            .search([("build_id", "=", build_id)], limit=1)
        )
        if not build:
            return {"error": "build not found"}

        status = kwargs.get("status")
        if status:
            vals = {
                "status": status,
                "completed_at": kwargs.get("completed_at") or False,
            }
            image_uri = kwargs.get("image_uri")
            if image_uri:
                vals["image_uri"] = image_uri
            error = kwargs.get("error")
            if error:
                vals["error_message"] = error
            final_logs = kwargs.get("final_logs")
            if final_logs:
                vals["progress"] = final_logs
            build.write(vals)
        else:
            progress = kwargs.get("progress")
            if progress:
                existing = build.progress or ""
                build.write({"progress": existing + progress})

        return {"ok": True}

    @http.route(
        "/kaiju/webhook/offsets",
        type="jsonrpc",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def get_offsets(self, **kwargs):
        if not self._verify_token():
            return {"error": "unauthorized"}

        builds = (
            request.env["kaiju.build"]
            .sudo()
            .search([("status", "in", ["queued", "building"])])
        )
        result = {}
        for b in builds:
            line_count = len((b.progress or "").splitlines())
            result[b.build_id] = line_count
        return result

    # ── API endpoints (auth=user) ───────────────────────────────────────

    @http.route("/api/build", type="jsonrpc", auth="user", methods=["POST"], csrf=False)
    def create_build(self, **kwargs):
        app_id = kwargs.get("app_id")
        if not app_id:
            return {"error": "missing app_id"}

        app = request.env["kaiju.app"].browse(int(app_id))
        if not app.exists():
            return {"error": "app not found"}

        repo_name = kwargs.get("repo_name")
        dataset_json = kwargs.get("dataset_json")
        if not repo_name or not dataset_json:
            return {"error": "repo_name and dataset_json are required"}

        build = request.env["kaiju.build"].create(
            {
                "app_id": app.id,
                "repo_name": repo_name,
                "dataset_json": dataset_json,
            }
        )
        build.action_build()

        return {
            "build_id": build.build_id,
            "app_name": build.app_name,
            "tag": build.tag,
            "status": build.status,
        }

    @http.route(
        "/api/build/<string:build_id>/status", type="http", auth="user", methods=["GET"]
    )
    def build_status(self, build_id, **kwargs):
        build = request.env["kaiju.build"].search(
            [("build_id", "=", build_id)], limit=1
        )
        if not build:
            return Response(
                json_lib.dumps({"error": "not found"}),
                status=404,
                content_type="application/json",
            )
        last_line = ""
        if build.progress:
            lines = build.progress.strip().splitlines()
            last_line = lines[-1] if lines else ""
        return Response(
            json_lib.dumps(
                {
                    "build_id": build.build_id,
                    "status": build.status,
                    "app_name": build.app_name,
                    "tag": build.tag,
                    "image_uri": build.image_uri or "",
                    "error_message": build.error_message or "",
                    "last_log_line": last_line,
                    "started_at": str(build.started_at) if build.started_at else "",
                    "completed_at": str(build.completed_at)
                    if build.completed_at
                    else "",
                }
            ),
            content_type="application/json",
        )

    @http.route(
        "/api/build/<string:build_id>/logs", type="http", auth="user", methods=["GET"]
    )
    def build_logs(self, build_id, **kwargs):
        build = request.env["kaiju.build"].search(
            [("build_id", "=", build_id)], limit=1
        )
        if not build:
            return Response(
                json_lib.dumps({"error": "not found"}),
                status=404,
                content_type="application/json",
            )
        return Response(
            json_lib.dumps(
                {
                    "build_id": build.build_id,
                    "logs": build.progress or "",
                }
            ),
            content_type="application/json",
        )

    @http.route("/api/builds", type="http", auth="user", methods=["GET"])
    def list_builds(self, **kwargs):
        limit = int(kwargs.get("limit", 20))
        offset = int(kwargs.get("offset", 0))
        builds = request.env["kaiju.build"].search(
            [], limit=limit, offset=offset, order="create_date desc"
        )
        return Response(
            json_lib.dumps(
                {
                    "builds": [
                        {
                            "build_id": b.build_id,
                            "app_name": b.app_name,
                            "status": b.status,
                            "tag": b.tag,
                            "image_uri": b.image_uri or "",
                            "started_at": str(b.started_at) if b.started_at else "",
                            "completed_at": str(b.completed_at)
                            if b.completed_at
                            else "",
                        }
                        for b in builds
                    ]
                }
            ),
            content_type="application/json",
        )
