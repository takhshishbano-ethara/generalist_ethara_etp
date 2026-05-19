"""Tests for ``services.extraction_service`` — URL validation + Lambda invoke.

boto3 is patched at the module level so no real AWS calls are issued.
"""
import socket
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services import extraction_service as svc


@tagged("post_install", "-at_install", "gohan")
class TestValidateUrl(TransactionCase):

    def test_empty_url_rejected(self):
        ok, err = svc.validate_url("")
        self.assertFalse(ok)
        self.assertIn("empty", err.lower())

    def test_whitespace_only_rejected(self):
        ok, _ = svc.validate_url("   ")
        self.assertFalse(ok)

    def test_invalid_scheme_rejected(self):
        ok, err = svc.validate_url("ftp://example.com")
        self.assertFalse(ok)
        self.assertIn("scheme", err.lower())

    def test_no_hostname_rejected(self):
        ok, err = svc.validate_url("https:///path")
        self.assertFalse(ok)
        self.assertIn("hostname", err.lower())

    def test_unresolvable_host_rejected(self):
        with patch.object(
            svc.socket, "getaddrinfo", side_effect=socket.gaierror,
        ):
            ok, err = svc.validate_url("https://this-does-not-resolve.invalid")
        self.assertFalse(ok)
        self.assertIn("Cannot resolve", err)

    def test_private_ip_rejected(self):
        with patch.object(
            svc.socket, "getaddrinfo",
            return_value=[(2, 1, 0, "", ("10.0.0.5", 0))],
        ):
            ok, err = svc.validate_url("https://internal.test")
        self.assertFalse(ok)
        self.assertIn("blocked", err.lower())

    def test_localhost_rejected(self):
        with patch.object(
            svc.socket, "getaddrinfo",
            return_value=[(2, 1, 0, "", ("127.0.0.1", 0))],
        ):
            ok, _ = svc.validate_url("https://localhost.test")
        self.assertFalse(ok)

    def test_link_local_rejected(self):
        with patch.object(
            svc.socket, "getaddrinfo",
            return_value=[(2, 1, 0, "", ("169.254.169.254", 0))],
        ):
            ok, _ = svc.validate_url("https://aws-metadata.test")
        self.assertFalse(ok)

    def test_public_ipv4_allowed(self):
        with patch.object(
            svc.socket, "getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 0))],
        ):
            ok, err = svc.validate_url("https://example.com")
        self.assertTrue(ok, err)


# ════════════════════════════════════════════════════════════════════
# _get_lambda_client (caching)
# ════════════════════════════════════════════════════════════════════


@tagged("post_install", "-at_install", "gohan")
class TestGetLambdaClient(TransactionCase):

    def setUp(self):
        super().setUp()
        # Reset cache so tests are independent
        with svc._CLIENT_LOCK:
            svc._CLIENT_CACHE.clear()

    def tearDown(self):
        with svc._CLIENT_LOCK:
            svc._CLIENT_CACHE.clear()
        super().tearDown()

    @patch.object(svc, "boto3")
    def test_caches_by_key(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(name="lambda-client")
        c1 = svc._get_lambda_client("us-east-1", "AKIA", "SECRET")
        c2 = svc._get_lambda_client("us-east-1", "AKIA", "SECRET")
        self.assertIs(c1, c2)
        self.assertEqual(mock_boto3.client.call_count, 1)

    @patch.object(svc, "boto3")
    def test_separate_clients_per_region(self, mock_boto3):
        mock_boto3.client.side_effect = [MagicMock(), MagicMock()]
        c1 = svc._get_lambda_client("us-east-1", "AKIA", "SECRET")
        c2 = svc._get_lambda_client("eu-west-1", "AKIA", "SECRET")
        self.assertIsNot(c1, c2)
        self.assertEqual(mock_boto3.client.call_count, 2)

    @patch.object(svc, "boto3")
    def test_no_credentials_uses_iam_role(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock()
        svc._get_lambda_client("us-east-1", "", "")
        kwargs = mock_boto3.client.call_args.kwargs
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertNotIn("aws_secret_access_key", kwargs)


@tagged("post_install", "-at_install", "gohan")
class TestTriggerExtraction(TransactionCase):

    def _public_url_resolver(self):
        return patch.object(
            svc.socket, "getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 0))],
        )

    def _fake_client(self, status_code=202, request_id="req-abc"):
        client = MagicMock()
        client.invoke.return_value = {
            "StatusCode": status_code,
            "ResponseMetadata": {"RequestId": request_id},
        }
        return client

    def test_invalid_url_short_circuits(self):
        result = svc.trigger_extraction(
            url="", job_id=1, callback_url="https://cb",
            function_name="fn", region="us-east-1",
        )
        self.assertFalse(result["success"])
        self.assertIn("URL validation", result["error"])

    def test_missing_function_name(self):
        with self._public_url_resolver():
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("Lambda function name", result["error"])

    def test_successful_invoke(self):
        client = self._fake_client()
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=42, callback_url="https://cb",
                function_name="lev-extractor", region="us-east-1",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["request_id"], "req-abc")
        client.invoke.assert_called_once()
        kwargs = client.invoke.call_args.kwargs
        self.assertEqual(kwargs["FunctionName"], "lev-extractor")
        self.assertEqual(kwargs["InvocationType"], "Event")

    def test_unexpected_status_code(self):
        client = self._fake_client(status_code=200)
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="fn", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("status 200", result["error"])

    def _client_with_error(self, code, message="boom"):
        client = MagicMock()
        client.invoke.side_effect = ClientError(
            {"Error": {"Code": code, "Message": message}}, "Invoke",
        )
        return client

    def test_too_many_requests_clear_message(self):
        client = self._client_with_error("TooManyRequestsException")
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="fn", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("concurrency", result["error"].lower())

    def test_resource_not_found(self):
        client = self._client_with_error("ResourceNotFoundException")
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="missing-fn", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("missing-fn", result["error"])

    def test_access_denied(self):
        client = self._client_with_error("AccessDeniedException")
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="fn", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("IAM denied", result["error"])

    def test_generic_exception_captured(self):
        client = MagicMock()
        client.invoke.side_effect = RuntimeError("network unreachable")
        with self._public_url_resolver(), patch.object(
            svc, "_get_lambda_client", return_value=client,
        ):
            result = svc.trigger_extraction(
                url="https://example.com", job_id=1, callback_url="https://cb",
                function_name="fn", region="us-east-1",
            )
        self.assertFalse(result["success"])
        self.assertIn("network unreachable", result["error"])
