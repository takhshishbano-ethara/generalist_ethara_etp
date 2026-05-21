"""S3 storage integrity (presigned URL + SHA-256) tests with mocked boto3."""
import hashlib
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "crowley")
class TestS3Integrity(TransactionCase):

    def _make_connector(self):
        return self.env["s3.connector"].sudo().create({
            "name": "test-bucket",
            "aws_access_key_id": "AKIA-TEST",
            "aws_secret_access_key": "secret-test",
            "region_name": "us-east-1",
            "cdn_url": "https://cdn.example.com",
        })

    def test_presigned_url_has_signature_query_params(self):
        connector = self._make_connector()
        storage = self.env["crowley.s3.storage"]
        with patch.object(type(storage), "_client_for") as mock_client_for:
            client = MagicMock()
            client.generate_presigned_url.return_value = (
                "https://test-bucket.s3.amazonaws.com/foo/bar.mp4"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
                "&X-Amz-Expires=300"
            )
            mock_client_for.return_value = client
            url = storage.presigned_get_url(connector.id, "foo/bar.mp4", expires_in=300)
        self.assertIn("X-Amz-Signature", url)
        self.assertIn("X-Amz-Expires", url)

    def test_verify_sha256_returns_remote_hash(self):
        connector = self._make_connector()
        storage = self.env["crowley.s3.storage"]
        # Build a fake S3 body that yields known bytes
        fake_body = MagicMock()
        fake_body.iter_chunks.return_value = iter([b"hello world"])
        with patch.object(type(storage), "_client_for") as mock_client_for:
            client = MagicMock()
            client.get_object.return_value = {"Body": fake_body}
            mock_client_for.return_value = client
            sha = storage.verify_object_sha256(connector.id, "foo/bar.mp4")
        expected = hashlib.sha256(b"hello world").hexdigest()
        self.assertEqual(sha, expected)
