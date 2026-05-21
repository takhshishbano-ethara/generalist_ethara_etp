# -*- coding: utf-8 -*-
"""Tests for the on-disk media refactor.

Covers:

* The FFmpeg processor's new path-returning ``_store``: render goes
  through ``shutil.move`` to ``<media_root>/<task.id>/...`` and the
  version row gets the relative path populated.  No new
  ir.attachment row is created for the render.
* The streaming controller's ``send_file`` path returns 206 Partial
  Content with a ``Content-Range`` header for ``Range:`` requests.
* The realpath + allowed-base guard rejects path-traversal attempts
  with a 404 (never the file contents).
* Odoo's access rules are honored: an unauthorized user gets
  404/403 instead of the bytes.
* Deleting a ``video.task`` purges its media directory.

The tests **monkey-patch** :func:`subprocess.run` so they don't need
the FFmpeg binary — they write a known byte sequence to the
``-i`` output path and return a fake CompletedProcess.  That's
enough to exercise the ``_store`` move + Char-field write path.
"""

import base64
import os
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged


# A 10-byte sentinel makes it trivial to assert that the Range
# response is serving real bytes from disk and not, e.g., an empty
# stub or an error page.
_FAKE_VIDEO_BYTES = b"VQCBYTES10"


def _fake_subprocess_run(command, **kwargs):
    """Drop-in replacement for ``subprocess.run`` that writes our
    sentinel bytes to whatever output path the FFmpeg command names.

    The command list always ends with the output filename (see
    ``ffmpeg_processor._build_command``), so we use ``command[-1]``.
    For ``ffprobe`` calls the last arg is the INPUT path — we still
    return a synthetic JSON blob with a duration field so ``_probe``
    is happy.
    """
    if command and command[0] == "ffprobe":
        # ``_probe`` calls ``json.loads(proc.stdout)``.
        return subprocess.CompletedProcess(
            command, returncode=0,
            stdout='{"format": {"duration": "1.0"}, "streams": [{"codec_type": "video", "width": 720, "height": 1280, "codec_name": "h264"}]}',
            stderr="",
        )
    output_path = command[-1]
    with open(output_path, "wb") as fh:
        fh.write(_FAKE_VIDEO_BYTES)
    return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")


@tagged("post_install", "-at_install", "video_qc")
class TestLocalStorageRender(TransactionCase):
    """Render-path tests — no HTTP, just ORM + filesystem assertions."""

    def setUp(self):
        super().setUp()
        # Point ``<media_root>`` at a per-test tempdir so we don't
        # pollute the real /var/lib/odoo location and so parallel
        # test workers don't collide.
        self._tmp_root = tempfile.mkdtemp(prefix="vqc_test_")
        self.addCleanup(shutil.rmtree, self._tmp_root, ignore_errors=True)
        self.env["ir.config_parameter"].sudo().set_param(
            "video_qc.media_root", self._tmp_root,
        )
        self.task = self.env["video.task"].create({"description": "render-test"})
        # Attach a tiny "source" so render_for_attachment has input
        # to dump.  Any bytes work — _fake_subprocess_run overwrites
        # the output anyway.
        self.task.original_video_1_attachment = self.env["ir.attachment"].create({
            "name": "src1.mp4",
            "datas": base64.b64encode(b"FAKESRC1BYTES"),
            "mimetype": "video/mp4",
        })
        self.version = self.task.create_new_version()

    def test_render_writes_file_to_disk_and_populates_path(self):
        # Configure trim window so _slot_should_render returns True.
        self.version.write_editing_config({
            "slot_1": {"trim": {"start": 0.0, "end": 5.0}, "crop": None},
        })
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            self.version._job_render()

        # File exists on disk at the canonical location.
        expected_abs = os.path.join(
            self._tmp_root,
            str(self.task.id),
            f"v{self.version.version_no}_edited_slot1.mp4",
        )
        self.assertTrue(
            os.path.isfile(expected_abs),
            f"Render output missing at {expected_abs}",
        )

        # The bytes are exactly what _fake_subprocess_run wrote.
        with open(expected_abs, "rb") as fh:
            self.assertEqual(fh.read(), _FAKE_VIDEO_BYTES)

        # The path field is populated with the relative path.
        self.version.invalidate_recordset()
        self.assertTrue(self.version.edited_file_1_path)
        self.assertEqual(
            self.version.edited_file_1_path,
            os.path.relpath(expected_abs, self._tmp_root),
        )

        # The legacy attachment column was cleared (we no longer write
        # to it; the controller falls back to it ONLY for pre-refactor
        # rows that the migration hook hasn't touched yet).
        self.assertFalse(self.version.edited_attachment_1_id)

        # No ir.attachment was created for this render — the only
        # attachment is the source we put on the task in setUp.
        attachments = self.env["ir.attachment"].search([
            ("video_version_id", "=", self.version.id),
        ])
        self.assertFalse(
            attachments,
            "Render must not create an ir.attachment row anymore; "
            f"got {attachments.mapped('name')}",
        )

    def test_unlinking_task_purges_media_dir(self):
        self.version.write_editing_config({
            "slot_1": {"trim": {"start": 0.0, "end": 5.0}, "crop": None},
        })
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            self.version._job_render()
        media_dir = os.path.join(self._tmp_root, str(self.task.id))
        self.assertTrue(os.path.isdir(media_dir))
        self.task.unlink()
        self.assertFalse(
            os.path.isdir(media_dir),
            f"Media dir {media_dir} should have been purged on task unlink.",
        )


@tagged("post_install", "-at_install", "video_qc")
class TestLocalStorageController(HttpCase):
    """HTTP-level tests — exercises the streaming controller end-to-end."""

    def setUp(self):
        super().setUp()
        self._tmp_root = tempfile.mkdtemp(prefix="vqc_http_test_")
        self.addCleanup(shutil.rmtree, self._tmp_root, ignore_errors=True)
        self.env["ir.config_parameter"].sudo().set_param(
            "video_qc.media_root", self._tmp_root,
        )
        self.task = self.env["video.task"].create({"description": "http-test"})
        self.task.original_video_1_attachment = self.env["ir.attachment"].create({
            "name": "src1.mp4",
            "datas": base64.b64encode(b"X"),
            "mimetype": "video/mp4",
        })
        self.version = self.task.create_new_version()
        self.version.write_editing_config({
            "slot_1": {"trim": {"start": 0.0, "end": 5.0}, "crop": None},
        })
        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            self.version._job_render()
        # Confirm the file is actually there before we exercise the
        # controller — otherwise the test would falsely "pass" by
        # virtue of the fallback returning 404 for a different reason.
        self.expected_abs = os.path.join(
            self._tmp_root, str(self.task.id),
            f"v{self.version.version_no}_edited_slot1.mp4",
        )
        assert os.path.isfile(self.expected_abs)
        self.env.cr.commit()  # so the HttpCase test client sees the row
        self.addCleanup(self._rollback_after_http)

    def _rollback_after_http(self):
        # Undo the explicit commit() above so the test database is
        # left clean for the next test.
        self.env["video.task"].search([("id", "=", self.task.id)]).unlink()

    def test_range_request_returns_206_with_content_range(self):
        self.authenticate("admin", "admin")
        url = f"/video_qc/version/{self.version.id}/edited/1"
        resp = self.url_open(url, headers={"Range": "bytes=0-9"})
        self.assertEqual(resp.status_code, 206, f"Expected 206, got {resp.status_code} body={resp.content!r}")
        self.assertIn("Content-Range", resp.headers)
        self.assertEqual(resp.headers["Content-Range"], "bytes 0-9/10")
        self.assertEqual(len(resp.content), 10)
        self.assertEqual(resp.content, _FAKE_VIDEO_BYTES)

    def test_path_traversal_returns_404(self):
        self.authenticate("admin", "admin")
        # Write a traversal-style relative path into the version row.
        self.version.sudo().write({
            "edited_file_1_path": "../../../etc/passwd",
        })
        self.env.cr.commit()
        url = f"/video_qc/version/{self.version.id}/edited/1"
        resp = self.url_open(url)
        self.assertEqual(
            resp.status_code, 404,
            "Path traversal must return 404, never the file contents.",
        )

    def test_unauthorized_user_does_not_get_bytes(self):
        """A user without read access on the task must NOT receive the file."""
        portal_user = self.env["res.users"].create({
            "name": "Portal Test",
            "login": "vqc_portal_test_user",
            "password": "test_pw_123!",
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
        })
        self.env.cr.commit()
        self.authenticate(portal_user.login, "test_pw_123!")
        url = f"/video_qc/version/{self.version.id}/edited/1"
        resp = self.url_open(url)
        self.assertIn(resp.status_code, (403, 404, 500),
                      f"Unauthorized user got status {resp.status_code}")
        # The most important assertion: NOT the file content.
        self.assertNotEqual(resp.content, _FAKE_VIDEO_BYTES)


@tagged("post_install", "-at_install", "video_qc")
class TestMediaStorageGuard(TransactionCase):
    """Direct unit tests for the realpath + allowed-base guard."""

    def setUp(self):
        super().setUp()
        self._tmp_root = tempfile.mkdtemp(prefix="vqc_guard_test_")
        self.addCleanup(shutil.rmtree, self._tmp_root, ignore_errors=True)
        self.env["ir.config_parameter"].sudo().set_param(
            "video_qc.media_root", self._tmp_root,
        )
        self.storage = self.env["video.qc.media.storage"].sudo()

    def test_absolute_rejects_parent_traversal(self):
        with self.assertRaises(UserError):
            self.storage.absolute("../etc/passwd")

    def test_absolute_rejects_absolute_outside_root(self):
        with self.assertRaises(UserError):
            self.storage.absolute("/etc/passwd")

    def test_absolute_accepts_valid_relative(self):
        # Create a file under the root and resolve it.
        sub = os.path.join(self._tmp_root, "42")
        os.makedirs(sub, exist_ok=True)
        target = os.path.join(sub, "v1_edited.mp4")
        with open(target, "wb") as fh:
            fh.write(b"ok")
        resolved = self.storage.absolute(os.path.relpath(target, self._tmp_root))
        self.assertEqual(resolved, os.path.realpath(target))

    def test_empty_path_rejected(self):
        with self.assertRaises(UserError):
            self.storage.absolute("")
