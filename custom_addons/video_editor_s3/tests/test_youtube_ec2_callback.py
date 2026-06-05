# -*- coding: utf-8 -*-
import json

from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install", "video_editor_s3")
class TestYoutubeEc2Callback(HttpCase):

    _route = "/video_editor_s3/callback/youtube_ec2"

    def setUp(self):
        super().setUp()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("video_editor_s3.aws_bucket", "test-bucket")
        ICP.set_param("video_editor_s3.aws_region", "us-east-1")
        ICP.set_param("video_editor_s3.aws_access_key", "AKIATEST")
        ICP.set_param("video_editor_s3.aws_secret_key", "secrettest")
        ICP.set_param("video_editor_s3.youtube_prefix", "test_yt")
        self.project = self.env["video.editor.project"].create({
            "s3_source_url": "s3://test-bucket/seed.mp4",
        })
        self.job = self.env["video.editor.job"].create({
            "project_id": self.project.id,
            "job_type": "youtube_ingest",
            "status": "running",
            "config_json": {
                "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "start_seconds": 10.0,
                "end_seconds": 20.0,
            },
        })
        self.env.cr.commit()

    def _post(self, payload):
        return self.url_open(
            self._route,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    def test_completed_callback_marks_job_done(self):
        s3_url = "https://ethara-text-to-video.s3.ap-south-1.amazonaws.com/youtube/%s/dQw4w9WgXcQ_10-20.mp4" % self.job.id
        resp = self._post({
            "job_id": "fastapi-internal-uuid-abc123",
            "status": "completed",
            "tasker_id": str(self.job.id),
            "video_id": "dQw4w9WgXcQ",
            "start_time": 10,
            "end_time": 20,
            "yt_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "s3_url": s3_url,
            "s3_key": "youtube/%s/dQw4w9WgXcQ_10-20.mp4" % self.job.id,
        })
        self.assertEqual(resp.status_code, 200)
        self.job.invalidate_recordset()
        self.project.invalidate_recordset()
        self.assertEqual(self.job.status, "done")
        self.assertEqual(self.job.output_s3_url, s3_url)
        self.assertEqual(self.project.output_s3_url, s3_url)

    def test_completed_callback_full_video_writes_s3_source_url(self):
        self.job.config_json = {
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "start_seconds": 0.0,
            "end_seconds": 0.0,
        }
        self.env.cr.commit()
        s3_url = "https://ethara-text-to-video.s3.ap-south-1.amazonaws.com/youtube/x/y.mp4"
        resp = self._post({
            "status": "completed",
            "tasker_id": str(self.job.id),
            "s3_url": s3_url,
        })
        self.assertEqual(resp.status_code, 200)
        self.project.invalidate_recordset()
        self.assertEqual(self.project.s3_source_url, s3_url)

    def test_failed_callback_marks_job_failed(self):
        resp = self._post({
            "tasker_id": str(self.job.id),
            "status": "failed",
            "error": "yt-dlp: HTTP 429 Too Many Requests",
        })
        self.assertEqual(resp.status_code, 200)
        self.job.invalidate_recordset()
        self.assertEqual(self.job.status, "failed")
        self.assertIn("429", self.job.error_message or "")

    def test_unknown_status_marks_job_failed(self):
        resp = self._post({
            "tasker_id": str(self.job.id),
            "status": "weird_state",
        })
        self.assertEqual(resp.status_code, 200)
        self.job.invalidate_recordset()
        self.assertEqual(self.job.status, "failed")
        self.assertIn("weird_state", self.job.error_message or "")

    def test_missing_tasker_id_returns_400(self):
        resp = self._post({"status": "completed", "s3_url": "https://x/y"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_tasker_id_returns_400(self):
        resp = self._post({"tasker_id": "not-a-number", "status": "completed"})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_tasker_id_returns_404(self):
        resp = self._post({"tasker_id": "999999999", "status": "completed"})
        self.assertEqual(resp.status_code, 404)

    def test_bad_json_returns_400(self):
        resp = self.url_open(
            self._route,
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_callback_on_done_job_is_idempotent(self):
        self.job.status = "done"
        self.job.output_s3_url = "https://existing/url"
        self.env.cr.commit()
        resp = self._post({
            "tasker_id": str(self.job.id),
            "status": "completed",
            "s3_url": "https://new/url",
        })
        self.assertEqual(resp.status_code, 200)
        self.job.invalidate_recordset()
        self.assertEqual(self.job.output_s3_url, "https://existing/url")
