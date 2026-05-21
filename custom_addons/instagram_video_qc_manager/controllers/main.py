# -*- coding: utf-8 -*-
"""HTTP endpoints used by the OWL video editor.

After the on-disk media refactor, every streaming route serves bytes
directly from the filesystem under ``<media_root>/<task.id>/`` via
:func:`werkzeug.utils.send_file` with ``conditional=True`` — that's
what gives the HTML5 ``<video>`` element real HTTP-Range scrubbing
(206 Partial Content with ``Content-Range:`` headers) instead of the
single in-memory blob the old controller produced.

Resolution order for every route:

1. Resolve the version / task row, run ``check_access_rights("read")``
   and ``check_access_rule("read")`` BEFORE any filesystem I/O.
2. Prefer the new on-disk path (``*_file_*_path`` Char column) when
   populated — go through :class:`video.qc.media.storage` for the
   realpath + allowed-base guard, then ``send_file``.
3. Fall back to the legacy ``ir.attachment`` row for rendered files
   produced before this refactor.  This branch keeps the old behaviour
   so existing tasks don't break.
4. Otherwise return 404.

URL shapes are unchanged — the OWL editor, kanban, and form views
all continue to point at the same routes.
"""

import base64
import logging
import mimetypes
import os

# Werkzeug ships ``send_file`` upstream from 2.0+; Odoo 19 vendors a
# copy in ``odoo.tools._vendor.send_file`` for older Werkzeug builds.
# We follow the same try/except dance Odoo's own ``http.py`` uses
# (see ``src/odoo/http.py:195-197``), so behaviour is identical in
# either environment.
try:
    from werkzeug.utils import send_file as _send_file
except ImportError:  # pragma: no cover — fallback for old Werkzeug
    from odoo.tools._vendor.send_file import send_file as _send_file

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
        # Source clips are always attachments (Instagram downloader),
        # never on-disk paths — they pre-date the refactor and the
        # downloader still creates ir.attachment rows.
        return self._stream(
            attachment=version.original_attachment_id or version.edited_attachment_id,
        )

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
        return self._stream(
            path=version.edited_file_path,
            attachment=version.edited_attachment_id,
            filename=f"v{version.version_no}_edited.mp4",
        )

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
        if slot == 1:
            return self._stream(
                path=version.edited_file_1_path,
                attachment=version.edited_attachment_1_id,
                filename=f"v{version.version_no}_edited_slot1.mp4",
            )
        return self._stream(
            path=version.edited_file_2_path,
            attachment=version.edited_attachment_2_id,
            filename=f"v{version.version_no}_edited_slot2.mp4",
        )

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
        version.check_access_rule("read")
        # Prefer dedicated preview render, fall back to the full
        # edited file (path-or-attachment), then to legacy attachment
        # rows for both.
        return self._stream(
            path=version.preview_file_path or version.edited_file_path,
            attachment=version.preview_attachment_id or version.edited_attachment_id,
            filename=f"v{version.version_no}_preview.mp4",
        )

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
        # Original sources still live as ir.attachment rows (the
        # Instagram downloader writes them) — no on-disk migration
        # is in-scope for THIS refactor (it covers FFmpeg outputs only).
        return self._stream(attachment=attachment)

    # ------------------------------------------------------------------
    # Streaming helper
    # ------------------------------------------------------------------
    def _stream(self, *, path=None, attachment=None, mimetype=None, filename=None):
        """Serve a video file.

        Prefers the on-disk ``path`` (relative, under ``<media_root>``)
        when populated.  Falls back to the legacy ``attachment`` row
        when ``path`` is empty.

        On-disk branch goes through ``werkzeug.utils.send_file`` with
        ``conditional=True`` + ``etag=True`` so HTML5 ``<video>``
        scrubbing produces real HTTP-Range requests (206 Partial
        Content with ``Content-Range``).  The legacy branch keeps the
        old in-memory ``make_response(bytes, ...)`` path for renders
        that were stored as attachments before this refactor — those
        clips don't get Range support but they keep playing.
        """
        if path:
            storage = request.env["video.qc.media.storage"].sudo()
            try:
                abs_path = storage.absolute(path)
            except (UserError, AccessError) as exc:
                # Either the stored relative path escapes ``<media_root>``
                # (path-traversal attempt) or the media root itself isn't
                # configured.  Don't leak the reason to the browser —
                # the user gets a clean 404.
                _logger.warning("Media path resolution rejected: %s", exc)
                return request.not_found()
            if not os.path.isfile(abs_path):
                _logger.info("Media file missing on disk: %s", abs_path)
                # If the path field references a file that was deleted
                # out-of-band, optionally fall back to the legacy
                # attachment row when the caller passed one — keeps
                # the user-facing UX graceful.
                if attachment:
                    return self._stream(attachment=attachment, mimetype=mimetype, filename=filename)
                return request.not_found()
            resolved_mimetype = (
                mimetype
                or mimetypes.guess_type(abs_path)[0]
                or "video/mp4"
            )
            # ``send_file`` handles If-Modified-Since, If-None-Match,
            # and Range automatically because we pass conditional=True
            # + etag=True.  We do NOT load the file into Python memory.
            return _send_file(
                abs_path,
                environ=request.httprequest.environ,
                mimetype=resolved_mimetype,
                download_name=filename or os.path.basename(abs_path),
                conditional=True,
                etag=True,
                last_modified=os.path.getmtime(abs_path),
            )
        if attachment:
            return self._stream_attachment_legacy(attachment, filename=filename)
        return request.not_found()

    def _stream_attachment_legacy(self, attachment, filename=None):
        """Stream a legacy ir.attachment-backed render.

        Pre-refactor renders were stored as base64 blobs in
        ``ir.attachment.datas``.  We keep this branch alive so those
        rows continue to play until the operator runs the post-init
        migration (which moves them onto the new on-disk layout).
        """
        if not attachment:
            return request.not_found()
        data = attachment.raw or (
            base64.b64decode(attachment.datas) if attachment.datas else b""
        )
        headers = [
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Length", str(len(data))),
            (
                "Content-Disposition",
                f'inline; filename="{filename or attachment.name}"',
            ),
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
            # Pre-flight check BEFORE we schedule the post-commit job.
            #
            # The render itself runs in a deferred new-cursor callback
            # AFTER this HTTP response is sent, so a missing ffmpeg
            # there would only surface as ``status=error`` on the
            # version row — invisible to the user clicking Save &
            # Render in the editor.  Resolving the binary
            # synchronously here means a missing install raises a
            # UserError on this RPC, which the OWL editor's
            # ``saveAndRender`` catches and shows in a danger
            # notification with the install instructions.
            processor = request.env["ffmpeg.processor"].sudo()
            processor._require_binary("ffmpeg")
            processor._require_binary("ffprobe")
            # Same pre-flight on the media root: a writable destination
            # is a non-negotiable precondition for the render.  This
            # also triggers MediaStorage's self-healing fallback if
            # the configured path is unwritable.
            request.env["video.qc.media.storage"].sudo().get_media_root()
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
