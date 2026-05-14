# -*- coding: utf-8 -*-
"""HTTP endpoints used by the OWL video editor.

* ``/video_qc/version/<id>/source`` streams the source attachment for a version.
* ``/video_qc/version/<id>/save_edit`` (JSON) persists an edit configuration
  coming from the editor and optionally queues a render.
* ``/video_qc/version/<id>/preview`` streams the most recent preview render.
* ``/video_qc/task/<id>/download_urls`` (JSON) lets the editor trigger the
  Instagram download manually.
"""

import base64
import json
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class VideoQCController(http.Controller):
    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    @http.route(
        "/video_qc/version/<int:version_id>/source",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def stream_version_source(self, version_id, **_kwargs):
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version:
            return request.not_found()
        version.check_access_rights("read")
        version.check_access_rule("read")
        attachment = version.original_attachment_id or version.edited_attachment_id
        return self._stream_attachment(attachment)

    @http.route(
        "/video_qc/version/<int:version_id>/edited",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def stream_version_edited(self, version_id, **_kwargs):
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version:
            return request.not_found()
        version.check_access_rights("read")
        version.check_access_rule("read")
        return self._stream_attachment(version.edited_attachment_id)

    @http.route(
        "/video_qc/version/<int:version_id>/edited/<int:slot>",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def stream_version_edited_slot(self, version_id, slot, **_kwargs):
        """Stream the per-slot trimmed output produced by a two-slot render."""
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version or slot not in (1, 2):
            return request.not_found()
        version.check_access_rights("read")
        version.check_access_rule("read")
        attachment = (
            version.edited_attachment_1_id if slot == 1 else version.edited_attachment_2_id
        )
        return self._stream_attachment(attachment)

    @http.route(
        "/video_qc/version/<int:version_id>/preview",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def stream_version_preview(self, version_id, **_kwargs):
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version:
            return request.not_found()
        version.check_access_rights("read")
        return self._stream_attachment(version.preview_attachment_id or version.edited_attachment_id)

    @http.route(
        "/video_qc/task/<int:task_id>/original/<int:slot>",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def stream_task_original(self, task_id, slot, **_kwargs):
        task = request.env["video.task"].browse(task_id).exists()
        if not task or slot not in (1, 2):
            return request.not_found()
        task.check_access_rights("read")
        task.check_access_rule("read")
        attachment = (
            task.original_video_1_attachment if slot == 1 else task.original_video_2_attachment
        )
        return self._stream_attachment(attachment)

    def _stream_attachment(self, attachment):
        if not attachment:
            return request.not_found()
        data = attachment.raw or (
            base64.b64decode(attachment.datas) if attachment.datas else b""
        )
        headers = [
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Length", str(len(data))),
            ("Content-Disposition", f'inline; filename="{attachment.name}"'),
            ("Cache-Control", "private, max-age=60"),
            ("Accept-Ranges", "bytes"),
        ]
        return request.make_response(data, headers=headers)

    # ------------------------------------------------------------------
    # JSON write endpoints used by the OWL editor
    # ------------------------------------------------------------------
    @http.route(
        "/video_qc/task/<int:task_id>/new_version",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def create_new_version(self, task_id, **kwargs):
        task = request.env["video.task"].browse(task_id).exists()
        if not task:
            raise UserError("Task not found.")
        version = task.create_new_version(vals={"edit_notes": kwargs.get("edit_notes")})
        return {"version_id": version.id, "version_no": version.version_no}

    @http.route(
        "/video_qc/version/<int:version_id>/save_edit",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def save_edit(self, version_id, config=None, render=False, **_kwargs):
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version:
            raise UserError("Version not found.")
        version.check_access_rights("write")
        version.check_access_rule("write")
        version.write_editing_config(config or {})
        if render:
            version.action_render()
        return {
            "ok": True,
            "version_id": version.id,
            "status": version.status,
        }

    @http.route(
        "/video_qc/version/<int:version_id>/save_prompt",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def save_prompt(self, version_id, prompt_text="", prompt_response="", **_kwargs):
        version = request.env["video.task.version"].browse(version_id).exists()
        if not version:
            raise UserError("Version not found.")
        version.check_access_rights("write")
        version.write({"prompt_text": prompt_text, "prompt_response": prompt_response})
        return {"ok": True}

    @http.route(
        "/video_qc/task/<int:task_id>/download",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def trigger_download(self, task_id, **_kwargs):
        task = request.env["video.task"].browse(task_id).exists()
        if not task:
            raise UserError("Task not found.")
        task.action_download_videos()
        return {"ok": True, "state": task.state}
