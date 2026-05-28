# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_editor_s3")
class TestYoutubeIngest(TransactionCase):

    def setUp(self):
        super().setUp()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("video_editor_s3.aws_bucket", "test-bucket")
        ICP.set_param("video_editor_s3.aws_region", "us-east-1")
        ICP.set_param("video_editor_s3.aws_access_key", "AKIATEST")
        ICP.set_param("video_editor_s3.aws_secret_key", "secrettest")
        ICP.set_param("video_editor_s3.youtube_prefix", "test_yt")

    def test_youtube_url_field_validation_accepts_valid(self):
        project = self.env["video.editor.project"].create({
            "s3_source_url": "s3://test-bucket/seed.mp4",
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        })
        self.assertEqual(project.youtube_video_id, "dQw4w9WgXcQ")

    def test_youtube_url_field_validation_rejects_invalid(self):
        with self.assertRaises(ValidationError):
            self.env["video.editor.project"].create({
                "s3_source_url": "s3://test-bucket/seed.mp4",
                "youtube_url": "https://vimeo.com/123",
            })

    def test_action_ingest_youtube_requires_url(self):
        from odoo.exceptions import UserError
        project = self.env["video.editor.project"].create({
            "s3_source_url": "s3://test-bucket/seed.mp4",
        })
        with self.assertRaises(UserError):
            project.action_ingest_youtube()

    def test_auto_ingest_on_create_with_youtube_url(self):
        project = self.env["video.editor.project"].create({
            "s3_source_url": "s3://test-bucket/seed.mp4",
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        })
        jobs = project.job_ids.filtered(lambda j: j.job_type == "youtube_ingest")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.status, "queued")
        self.assertEqual(
            jobs.config_json.get("youtube_url"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
