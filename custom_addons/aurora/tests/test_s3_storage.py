# -*- coding: utf-8 -*-
import os
import re
import time
from unittest import TestCase
from unittest.mock import patch, MagicMock, call


class TestIsConfigured(TestCase):

    def test_bucket_present_returns_true(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertTrue(is_configured({"bucket": "my-bucket"}))

    def test_empty_bucket_returns_false(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertFalse(is_configured({"bucket": ""}))

    def test_missing_bucket_key_returns_false(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertFalse(is_configured({}))

    def test_none_bucket_returns_false(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertFalse(is_configured({"bucket": None}))

    def test_whitespace_bucket_returns_true(self):
        from odoo.addons.aurora.models.s3_storage import is_configured
        self.assertTrue(is_configured({"bucket": " "}))


class TestBuildBasePrefix(TestCase):

    def test_no_folder_no_phase(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1")
        self.assertEqual(result, "aurora_phase1/org1__repo1/")

    def test_with_folder(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", folder="aurora")
        self.assertEqual(result, "aurora/aurora_phase1/org1__repo1/")

    def test_with_phase(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", phase="aurora_phase2")
        self.assertEqual(result, "aurora_phase2/org1__repo1/")

    def test_with_folder_and_phase(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", folder="data", phase="phase3")
        self.assertEqual(result, "data/phase3/org1__repo1/")

    def test_folder_with_slashes_stripped(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", folder="/aurora/")
        self.assertEqual(result, "aurora/aurora_phase1/org1__repo1/")

    def test_phase_with_slashes_stripped(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", phase="/phase2/")
        self.assertEqual(result, "phase2/org1__repo1/")

    def test_empty_folder_treated_as_none(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", folder="")
        self.assertEqual(result, "aurora_phase1/org1__repo1/")

    def test_empty_phase_uses_default(self):
        from odoo.addons.aurora.models.s3_storage import _build_base_prefix
        result = _build_base_prefix("org1", "repo1", phase="")
        self.assertEqual(result, "aurora_phase1/org1__repo1/")


class TestBuildS3Key(TestCase):

    def test_basic_key(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 1, "file.jsonl")
        self.assertEqual(result, "aurora_phase1/org__repo/run_1/file.jsonl")

    def test_with_folder_and_phase(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("org", "repo", 5, "data.json", folder="aurora", phase="phase2")
        self.assertEqual(result, "aurora/phase2/org__repo/run_5/data.json")

    def test_run_number_in_path(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("a", "b", 42, "x.txt")
        self.assertIn("run_42/", result)

    def test_filename_at_end(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("a", "b", 1, "myfile.tar")
        self.assertTrue(result.endswith("myfile.tar"))

    def test_nested_filename(self):
        from odoo.addons.aurora.models.s3_storage import build_s3_key
        result = build_s3_key("a", "b", 1, "sub/nested.txt")
        self.assertIn("run_1/sub/nested.txt", result)


class TestGetClient(TestCase):

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_creates_client_with_region(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({"region": "eu-west-1"})
        mock_client.assert_called_once()
        kwargs = mock_client.call_args[1]
        self.assertEqual(kwargs["region_name"], "eu-west-1")

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_default_region_us_east_1(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({})
        kwargs = mock_client.call_args[1]
        self.assertEqual(kwargs["region_name"], "us-east-1")

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_with_access_keys(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({"access_key": "AK", "secret_key": "SK"})
        kwargs = mock_client.call_args[1]
        self.assertEqual(kwargs["aws_access_key_id"], "AK")
        self.assertEqual(kwargs["aws_secret_access_key"], "SK")

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_without_access_keys(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({"region": "us-east-1"})
        kwargs = mock_client.call_args[1]
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertNotIn("aws_secret_access_key", kwargs)

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_empty_access_key_not_used(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({"access_key": "", "secret_key": ""})
        kwargs = mock_client.call_args[1]
        self.assertNotIn("aws_access_key_id", kwargs)

    @patch("boto3.client")
    @patch("botocore.config.Config")
    def test_endpoint_url_includes_region(self, mock_config, mock_client):
        from odoo.addons.aurora.models.s3_storage import _get_client
        _get_client({"region": "ap-south-1"})
        kwargs = mock_client.call_args[1]
        self.assertNotIn("endpoint_url", kwargs)
        self.assertEqual(kwargs["region_name"], "ap-south-1")


class TestGetTransferConfig(TestCase):

    @patch("boto3.s3.transfer.TransferConfig")
    def test_returns_transfer_config(self, mock_tc):
        from odoo.addons.aurora.models.s3_storage import _get_transfer_config
        _get_transfer_config()
        mock_tc.assert_called_once()

    @patch("boto3.s3.transfer.TransferConfig")
    def test_multipart_threshold_50mb(self, mock_tc):
        from odoo.addons.aurora.models.s3_storage import _get_transfer_config
        _get_transfer_config()
        kwargs = mock_tc.call_args[1]
        self.assertEqual(kwargs["multipart_threshold"], 50 * 1024 * 1024)

    @patch("boto3.s3.transfer.TransferConfig")
    def test_multipart_chunksize_25mb(self, mock_tc):
        from odoo.addons.aurora.models.s3_storage import _get_transfer_config
        _get_transfer_config()
        kwargs = mock_tc.call_args[1]
        self.assertEqual(kwargs["multipart_chunksize"], 25 * 1024 * 1024)

    @patch("boto3.s3.transfer.TransferConfig")
    def test_max_concurrency_4(self, mock_tc):
        from odoo.addons.aurora.models.s3_storage import _get_transfer_config
        _get_transfer_config()
        kwargs = mock_tc.call_args[1]
        self.assertEqual(kwargs["max_concurrency"], 4)

    @patch("boto3.s3.transfer.TransferConfig")
    def test_use_threads_true(self, mock_tc):
        from odoo.addons.aurora.models.s3_storage import _get_transfer_config
        _get_transfer_config()
        kwargs = mock_tc.call_args[1]
        self.assertTrue(kwargs["use_threads"])


class TestValidateCredentials(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_calls_head_bucket(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import validate_credentials
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        validate_credentials({"bucket": "test-bucket", "region": "us-east-1"})
        mock_client.head_bucket.assert_called_once_with(Bucket="test-bucket")

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_raises_on_invalid_bucket(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import validate_credentials
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = Exception("403")
        mock_gc.return_value = mock_client
        with self.assertRaises(Exception):
            validate_credentials({"bucket": "bad-bucket"})

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_passes_s3_config_to_get_client(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import validate_credentials
        mock_gc.return_value = MagicMock()
        cfg = {"bucket": "b", "region": "r"}
        validate_credentials(cfg)
        mock_gc.assert_called_once_with(cfg)


class TestGetNextRunNumber(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_no_existing_runs_returns_1(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"CommonPrefixes": []}]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 1)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_existing_run_3_returns_4(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        prefix = "aurora_phase1/org__repo/"
        paginator.paginate.return_value = [{
            "CommonPrefixes": [
                {"Prefix": f"{prefix}run_1/"},
                {"Prefix": f"{prefix}run_3/"},
                {"Prefix": f"{prefix}run_2/"},
            ]
        }]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 4)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_ignores_non_run_prefixes(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        prefix = "aurora_phase1/org__repo/"
        paginator.paginate.return_value = [{
            "CommonPrefixes": [
                {"Prefix": f"{prefix}run_2/"},
                {"Prefix": f"{prefix}other_dir/"},
                {"Prefix": f"{prefix}backup/"},
            ]
        }]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 3)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_empty_pages(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 1)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_uses_folder_and_phase(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"CommonPrefixes": []}]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo", folder="f", phase="p")
        kwargs = paginator.paginate.call_args[1]
        self.assertEqual(kwargs["Prefix"], "f/p/org__repo/")

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_multiple_pages(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import get_next_run_number
        mock_client = MagicMock()
        paginator = MagicMock()
        prefix = "aurora_phase1/org__repo/"
        paginator.paginate.return_value = [
            {"CommonPrefixes": [{"Prefix": f"{prefix}run_1/"}]},
            {"CommonPrefixes": [{"Prefix": f"{prefix}run_5/"}]},
        ]
        mock_client.get_paginator.return_value = paginator
        mock_gc.return_value = mock_client
        result = get_next_run_number({"bucket": "b", "region": "r"}, "org", "repo")
        self.assertEqual(result, 6)


class TestUploadFile(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_success_returns_url(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        url = upload_file({"bucket": "b", "region": "us-east-1"}, "/tmp/f.txt", "key/f.txt")
        self.assertEqual(url, "https://b.s3.us-east-1.amazonaws.com/key/f.txt")

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_calls_upload_file_on_client(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        mock_client.upload_file.assert_called_once()

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_retries_on_failure(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = [Exception("net"), Exception("net"), None]
        mock_gc.return_value = mock_client
        upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        self.assertEqual(mock_client.upload_file.call_count, 3)

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_raises_after_max_attempts(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = Exception("fail")
        mock_gc.return_value = mock_client
        with self.assertRaises(Exception):
            upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        self.assertEqual(mock_client.upload_file.call_count, 3)

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_backoff_between_retries(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_client.upload_file.side_effect = [Exception("e"), Exception("e"), None]
        mock_gc.return_value = mock_client
        upload_file({"bucket": "b", "region": "r"}, "/tmp/f.txt", "k")
        self.assertEqual(mock_sleep.call_args_list, [call(4), call(8)])

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=100 * 1024 * 1024)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_large_file_uses_multipart(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        upload_file({"bucket": "b", "region": "r"}, "/tmp/big.tar", "k")
        mock_client.upload_file.assert_called_once()

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=500)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_default_region_ap_south_1(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        url = upload_file({"bucket": "b"}, "/tmp/f.txt", "k")
        self.assertIn("ap-south-1", url)


class TestGeneratePresignedUrl(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_calls_generate_presigned_url(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://presigned"
        mock_gc.return_value = mock_client
        result = generate_presigned_url({"bucket": "b", "region": "r"}, "key/path")
        self.assertEqual(result, "https://presigned")

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_default_expiry_3600(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        generate_presigned_url({"bucket": "b", "region": "r"}, "k")
        kwargs = mock_client.generate_presigned_url.call_args
        self.assertEqual(kwargs[1]["ExpiresIn"], 3600)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_custom_expiry(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        generate_presigned_url({"bucket": "b", "region": "r"}, "k", expires_in=7200)
        kwargs = mock_client.generate_presigned_url.call_args
        self.assertEqual(kwargs[1]["ExpiresIn"], 7200)

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_passes_bucket_and_key(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        generate_presigned_url({"bucket": "mybucket", "region": "r"}, "my/key.json")
        kwargs = mock_client.generate_presigned_url.call_args
        self.assertEqual(kwargs[1]["Params"]["Bucket"], "mybucket")
        self.assertEqual(kwargs[1]["Params"]["Key"], "my/key.json")

    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_uses_get_object_method(self, mock_gc):
        from odoo.addons.aurora.models.s3_storage import generate_presigned_url
        mock_client = MagicMock()
        mock_gc.return_value = mock_client
        generate_presigned_url({"bucket": "b", "region": "r"}, "k")
        args = mock_client.generate_presigned_url.call_args[0]
        self.assertEqual(args[0], "get_object")


class TestRunPrefixRe(TestCase):

    def test_matches_run_1(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_1/")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1")

    def test_matches_run_999(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_999/")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "999")

    def test_no_match_without_trailing_slash(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_1")
        self.assertIsNone(m)

    def test_no_match_non_numeric(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("run_abc/")
        self.assertIsNone(m)

    def test_no_match_other_prefix(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("backup_1/")
        self.assertIsNone(m)

    def test_no_match_empty_string(self):
        from odoo.addons.aurora.models.s3_storage import _RUN_PREFIX_RE
        m = _RUN_PREFIX_RE.match("")
        self.assertIsNone(m)


class TestConstants(TestCase):

    def test_max_upload_attempts_is_3(self):
        from odoo.addons.aurora.models.s3_storage import _S3_MAX_UPLOAD_ATTEMPTS
        self.assertEqual(_S3_MAX_UPLOAD_ATTEMPTS, 3)

    def test_retry_backoff_base_is_4(self):
        from odoo.addons.aurora.models.s3_storage import _S3_RETRY_BACKOFF_BASE
        self.assertEqual(_S3_RETRY_BACKOFF_BASE, 4)

    def test_multipart_threshold_50mb(self):
        from odoo.addons.aurora.models.s3_storage import _S3_MULTIPART_THRESHOLD
        self.assertEqual(_S3_MULTIPART_THRESHOLD, 50 * 1024 * 1024)

    def test_multipart_chunksize_25mb(self):
        from odoo.addons.aurora.models.s3_storage import _S3_MULTIPART_CHUNKSIZE
        self.assertEqual(_S3_MULTIPART_CHUNKSIZE, 25 * 1024 * 1024)

    def test_max_concurrency_4(self):
        from odoo.addons.aurora.models.s3_storage import _S3_MAX_CONCURRENCY
        self.assertEqual(_S3_MAX_CONCURRENCY, 4)


# =============================================================================
# build_url — endpoint override logic
# =============================================================================

class TestBuildUrl(TestCase):

    def test_default_returns_aws_url(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AURORA_S3_ENDPOINT", None)
            result = build_url("my-bucket", "us-east-1", "aurora/key.json")
        self.assertEqual(result, "https://my-bucket.s3.us-east-1.amazonaws.com/aurora/key.json")

    def test_endpoint_override_returns_custom_url(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://minio.local:9000"}):
            result = build_url("my-bucket", "us-east-1", "aurora/key.json")
        self.assertEqual(result, "http://minio.local:9000/my-bucket/aurora/key.json")

    def test_endpoint_override_trailing_slash_stripped(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://minio.local:9000/"}):
            result = build_url("bucket", "r", "k")
        self.assertEqual(result, "http://minio.local:9000/bucket/k")

    def test_empty_endpoint_uses_aws(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": ""}):
            result = build_url("b", "eu-west-1", "k")
        self.assertEqual(result, "https://b.s3.eu-west-1.amazonaws.com/k")

    def test_whitespace_endpoint_uses_aws(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "   "}):
            result = build_url("b", "ap-south-1", "path/file.jsonl")
        self.assertEqual(result, "https://b.s3.ap-south-1.amazonaws.com/path/file.jsonl")

    def test_endpoint_with_path_preserved(self):
        from odoo.addons.aurora.models.s3_storage import build_url
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://host:9000"}):
            result = build_url("bkt", "r", "folder/phase/org__repo/run_1/file.jsonl")
        self.assertEqual(result, "http://host:9000/bkt/folder/phase/org__repo/run_1/file.jsonl")


class TestUploadFileUseBuildUrl(TestCase):

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_upload_returns_minio_url_when_endpoint_set(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_gc.return_value = MagicMock()
        with patch.dict(os.environ, {"AURORA_S3_ENDPOINT": "http://minio:9000"}):
            url = upload_file({"bucket": "bkt", "region": "us-east-1"}, "/tmp/f.txt", "key/f.txt")
        self.assertEqual(url, "http://minio:9000/bkt/key/f.txt")

    @patch("odoo.addons.aurora.models.s3_storage.time.sleep")
    @patch("odoo.addons.aurora.models.s3_storage.os.path.getsize", return_value=1000)
    @patch("odoo.addons.aurora.models.s3_storage._get_transfer_config")
    @patch("odoo.addons.aurora.models.s3_storage._get_client")
    def test_upload_returns_aws_url_when_no_endpoint(self, mock_gc, mock_tc, mock_size, mock_sleep):
        from odoo.addons.aurora.models.s3_storage import upload_file
        mock_gc.return_value = MagicMock()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AURORA_S3_ENDPOINT", None)
            url = upload_file({"bucket": "bkt", "region": "eu-west-1"}, "/tmp/f.txt", "key/f.txt")
        self.assertEqual(url, "https://bkt.s3.eu-west-1.amazonaws.com/key/f.txt")
