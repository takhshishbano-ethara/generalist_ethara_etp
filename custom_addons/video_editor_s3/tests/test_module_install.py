# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_editor_s3")
class TestModuleInstall(TransactionCase):

    def test_models_registered(self):
        for model in (
            "video.editor.project",
            "video.editor.job",
            "video.editor.processing.log",
            "video.editor.s3.media.storage",
            "video.editor.s3.ffmpeg.processor",
            "video.editor.s3.settings",
        ):
            self.assertIn(model, self.env)

    def test_groups_exist(self):
        for xmlid in (
            "video_editor_s3.group_video_editor_s3_user",
            "video_editor_s3.group_video_editor_s3_manager",
        ):
            self.assertTrue(self.env.ref(xmlid, raise_if_not_found=False), xmlid)

    def test_action_exists(self):
        self.assertTrue(self.env.ref("video_editor_s3.action_video_editor_client", raise_if_not_found=False))

    def test_create_project(self):
        project = self.env["video.editor.project"].create({
            "s3_source_url": "s3://test-bucket/path/sample.mp4",
        })
        self.assertTrue(project.id)
        self.assertEqual(project.state, "draft")
        self.assertEqual(project.s3_source_key, "path/sample.mp4")

    def test_invalid_url_rejected(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env["video.editor.project"].create({"s3_source_url": "not-a-url"})
