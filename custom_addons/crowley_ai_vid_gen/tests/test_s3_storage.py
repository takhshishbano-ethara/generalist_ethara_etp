# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "crowley_ai_vid_gen")
class TestS3Storage(TransactionCase):

    def setUp(self):
        super().setUp()
        self.s3 = self.env["crowley.ai.vid.gen.s3.storage"]
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("crowley_ai_vid_gen.s3_bucket", "crowley-test")
        ICP.set_param("crowley_ai_vid_gen.s3_region", "us-east-1")
        ICP.set_param("crowley_ai_vid_gen.s3_access_key", "AKIA-TEST")
        ICP.set_param("crowley_ai_vid_gen.s3_secret_key", "secret-test")
        ICP.set_param("crowley_ai_vid_gen.s3_endpoint_url", "")
        # Ensure clean cache between tests
        self.s3.clear_cache()

    def tearDown(self):
        self.s3.clear_cache()
        super().tearDown()

    def test_bucket_missing_raises(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "crowley_ai_vid_gen.s3_bucket", "")
        with self.assertRaises(UserError):
            self.s3._bucket()

    def test_bucket_returns_configured_value(self):
        self.assertEqual(self.s3._bucket(), "crowley-test")

    @patch("odoo.addons.crowley_ai_vid_gen.services.s3_storage.boto3.client")
    def test_client_caches(self, mock_boto):
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        c1 = self.s3._client()
        c2 = self.s3._client()
        self.assertIs(c1, c2, "Repeated _client() calls must return the cached instance")
        mock_boto.assert_called_once()

    @patch("odoo.addons.crowley_ai_vid_gen.services.s3_storage.boto3.client")
    def test_client_recreated_after_clear_cache(self, mock_boto):
        mock_boto.return_value = MagicMock()
        self.s3._client()
        self.s3.clear_cache()
        self.s3._client()
        self.assertEqual(mock_boto.call_count, 2)

    @patch("odoo.addons.crowley_ai_vid_gen.services.s3_storage.boto3.client")
    def test_upload_fileobj_sets_correct_extra_args(self, mock_boto):
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ETag": '"abc123"'}
        mock_boto.return_value = mock_client
        etag = self.s3.upload_fileobj(
            MagicMock(), bucket="crowley-test", key="path/to/v.mp4",
            mimetype="video/mp4",
        )
        self.assertEqual(etag, "abc123")  # quotes stripped
        _, kwargs = mock_client.upload_fileobj.call_args
        self.assertEqual(kwargs["Bucket"], "crowley-test")
        self.assertEqual(kwargs["Key"], "path/to/v.mp4")
        self.assertEqual(kwargs["ExtraArgs"]["ContentType"], "video/mp4")
        self.assertEqual(kwargs["ExtraArgs"]["ContentDisposition"], "inline")

    @patch("odoo.addons.crowley_ai_vid_gen.services.s3_storage.boto3.client")
    def test_presigned_get_url(self, mock_boto):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://s3.example/signed-url"
        mock_boto.return_value = mock_client
        url = self.s3.presigned_get_url(
            "videos/x.mp4", expires_in=300,
            mimetype="video/mp4", disposition="inline", filename="My Video.mp4",
        )
        self.assertEqual(url, "https://s3.example/signed-url")
        _, kwargs = mock_client.generate_presigned_url.call_args
        self.assertEqual(kwargs["ExpiresIn"], 300)
        self.assertEqual(kwargs["Params"]["Bucket"], "crowley-test")
        self.assertEqual(kwargs["Params"]["ResponseContentType"], "video/mp4")
        self.assertIn("My Video.mp4", kwargs["Params"]["ResponseContentDisposition"])
