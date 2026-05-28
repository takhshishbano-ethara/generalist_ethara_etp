# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.video_editor_s3.services import s3_storage


@tagged("post_install", "-at_install", "video_editor_s3")
class TestS3StorageHelpers(TransactionCase):

    def test_parse_s3_scheme(self):
        bucket, key = s3_storage.parse_s3_url("s3://my-bucket/path/to/object.mp4")
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "path/to/object.mp4")

    def test_parse_virtual_host_style(self):
        url = "https://my-bucket.s3.ap-south-1.amazonaws.com/folder/video.mp4"
        bucket, key = s3_storage.parse_s3_url(url)
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "folder/video.mp4")

    def test_parse_path_style(self):
        url = "https://s3.us-east-1.amazonaws.com/my-bucket/folder/video.mp4"
        bucket, key = s3_storage.parse_s3_url(url)
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "folder/video.mp4")

    def test_parse_invalid_returns_none(self):
        bucket, key = s3_storage.parse_s3_url("not a url at all")
        self.assertFalse(bucket)
        self.assertFalse(key)

    def test_parse_s3_scheme_requires_key(self):
        bucket, key = s3_storage.parse_s3_url("s3://only-bucket")
        self.assertFalse(bucket or key)

    def test_build_url_default(self):
        url = s3_storage.build_url("my-bucket", "ap-south-1", "folder/video.mp4")
        self.assertEqual(
            url, "https://my-bucket.s3.ap-south-1.amazonaws.com/folder/video.mp4"
        )

    def test_is_configured(self):
        self.assertFalse(s3_storage.is_configured({}))
        self.assertFalse(s3_storage.is_configured({"bucket": ""}))
        self.assertTrue(s3_storage.is_configured({"bucket": "my-bucket"}))

    def test_settings_resolver_defaults(self):
        cfg = self.env["video.editor.s3.settings"].get_s3_config()
        self.assertIn("bucket", cfg)
        self.assertIn("region", cfg)
        self.assertEqual(cfg["region"], "ap-south-1")
        self.assertIsInstance(
            self.env["video.editor.s3.settings"].get_max_source_size_bytes(), int
        )
        self.assertGreaterEqual(
            self.env["video.editor.s3.settings"].get_max_concurrent_jobs(), 1
        )
