# -*- coding: utf-8 -*-
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, call, ANY


class TestInstanceIdFor(TestCase):

    def test_basic_format(self):
        from odoo.addons.aurora.models.artifact_collector import instance_id_for
        pr = MagicMock(org="myorg", repo="myrepo", number=42)
        self.assertEqual(instance_id_for(pr), "myorg__myrepo-pr-42")

    def test_different_values(self):
        from odoo.addons.aurora.models.artifact_collector import instance_id_for
        pr = MagicMock(org="a", repo="b", number=1)
        self.assertEqual(instance_id_for(pr), "a__b-pr-1")

    def test_large_number(self):
        from odoo.addons.aurora.models.artifact_collector import instance_id_for
        pr = MagicMock(org="x", repo="y", number=99999)
        self.assertEqual(instance_id_for(pr), "x__y-pr-99999")

    def test_hyphenated_repo(self):
        from odoo.addons.aurora.models.artifact_collector import instance_id_for
        pr = MagicMock(org="org", repo="my-repo", number=5)
        self.assertEqual(instance_id_for(pr), "org__my-repo-pr-5")

    def test_dotted_org(self):
        from odoo.addons.aurora.models.artifact_collector import instance_id_for
        pr = MagicMock(org="my.org", repo="repo", number=10)
        self.assertEqual(instance_id_for(pr), "my.org__repo-pr-10")


class TestReadTail(TestCase):

    def test_small_file_returns_full_content(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("hello world")
            path = f.name
        try:
            result = _read_tail(path)
            self.assertEqual(result, "hello world")
        finally:
            os.unlink(path)

    def test_large_file_returns_tail(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail, _LOG_TAIL_BYTES
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False) as f:
            f.write(b"x" * (_LOG_TAIL_BYTES + 1000))
            path = f.name
        try:
            result = _read_tail(path)
            self.assertEqual(len(result), _LOG_TAIL_BYTES)
        finally:
            os.unlink(path)

    def test_none_path_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        self.assertIsNone(_read_tail(None))

    def test_empty_path_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        self.assertIsNone(_read_tail(""))

    def test_nonexistent_file_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        self.assertIsNone(_read_tail("/nonexistent/path/file.log"))

    def test_custom_max_bytes(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False) as f:
            f.write(b"a" * 100)
            path = f.name
        try:
            result = _read_tail(path, max_bytes=10)
            self.assertEqual(len(result), 10)
        finally:
            os.unlink(path)

    def test_exact_max_bytes_no_seek(self):
        from odoo.addons.aurora.models.artifact_collector import _read_tail
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False) as f:
            f.write(b"z" * 50)
            path = f.name
        try:
            result = _read_tail(path, max_bytes=50)
            self.assertEqual(len(result), 50)
        finally:
            os.unlink(path)


class TestReadCapped(TestCase):

    def test_small_file_returns_full(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello")
            path = f.name
        try:
            self.assertEqual(_read_capped(path, 1000), "hello")
        finally:
            os.unlink(path)

    def test_file_over_cap_truncated(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("a" * 100)
            path = f.name
        try:
            result = _read_capped(path, 10)
            self.assertIn("[truncated]", result)
            self.assertTrue(len(result) < 100)
        finally:
            os.unlink(path)

    def test_none_path_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        self.assertIsNone(_read_capped(None, 100))

    def test_empty_path_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        self.assertIsNone(_read_capped("", 100))

    def test_nonexistent_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        self.assertIsNone(_read_capped("/no/such/file.txt", 100))

    def test_exact_cap_not_truncated(self):
        from odoo.addons.aurora.models.artifact_collector import _read_capped
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("a" * 10)
            path = f.name
        try:
            result = _read_capped(path, 10)
            self.assertEqual(result, "a" * 10)
            self.assertNotIn("[truncated]", result)
        finally:
            os.unlink(path)


class TestUploadArtifact(TestCase):

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_uploads_existing_file(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import _upload_artifact
        mock_s3.upload_file.return_value = "https://url"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            result = _upload_artifact({"bucket": "b"}, True, path, "key")
            self.assertEqual(result, "https://url")
        finally:
            os.unlink(path)

    def test_use_s3_false_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _upload_artifact
        result = _upload_artifact({"bucket": "b"}, False, "/tmp/f.txt", "key")
        self.assertIsNone(result)

    def test_none_path_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _upload_artifact
        result = _upload_artifact({"bucket": "b"}, True, None, "key")
        self.assertIsNone(result)

    def test_nonexistent_file_returns_none(self):
        from odoo.addons.aurora.models.artifact_collector import _upload_artifact
        result = _upload_artifact({"bucket": "b"}, True, "/no/such/file", "key")
        self.assertIsNone(result)

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_upload_exception_returns_none(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import _upload_artifact
        mock_s3.upload_file.side_effect = Exception("network")
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            result = _upload_artifact({"bucket": "b"}, True, path, "key")
            self.assertIsNone(result)
        finally:
            os.unlink(path)


class TestBuildInstanceKey(TestCase):

    def test_basic_key(self):
        from odoo.addons.aurora.models.artifact_collector import _build_instance_key
        result = _build_instance_key("folder", "phase2", "org", "repo", 1, "inst-1", "file.log")
        self.assertIn("inst-1/file.log", result)

    def test_includes_run_number(self):
        from odoo.addons.aurora.models.artifact_collector import _build_instance_key
        result = _build_instance_key("", "p1", "o", "r", 5, "i", "f")
        self.assertIn("run_5/", result)

    def test_nested_path(self):
        from odoo.addons.aurora.models.artifact_collector import _build_instance_key
        result = _build_instance_key("aurora", "phase2", "a", "b", 3, "x__y-pr-1", "report.json")
        self.assertIn("x__y-pr-1/report.json", result)


class TestAllowedInstanceColumns(TestCase):

    def test_status_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("status", _ALLOWED_INSTANCE_COLUMNS)

    def test_resolved_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("resolved", _ALLOWED_INSTANCE_COLUMNS)

    def test_f2p_count_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("f2p_count", _ALLOWED_INSTANCE_COLUMNS)

    def test_oci_tar_s3_uri_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("oci_tar_s3_uri", _ALLOWED_INSTANCE_COLUMNS)

    def test_random_col_not_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertNotIn("hacker_field", _ALLOWED_INSTANCE_COLUMNS)

    def test_id_not_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertNotIn("id", _ALLOWED_INSTANCE_COLUMNS)

    def test_evaluation_id_not_allowed(self):
        from odoo.addons.aurora.models.artifact_collector import _ALLOWED_INSTANCE_COLUMNS
        self.assertNotIn("evaluation_id", _ALLOWED_INSTANCE_COLUMNS)


class TestUpdateInstance(TestCase):

    def test_empty_vals_no_op(self):
        from odoo.addons.aurora.models.artifact_collector import update_instance
        conn = MagicMock()
        update_instance(conn, 1, {})
        conn.cursor.assert_not_called()

    def test_disallowed_column_raises(self):
        from odoo.addons.aurora.models.artifact_collector import update_instance
        conn = MagicMock()
        with self.assertRaises(ValueError):
            update_instance(conn, 1, {"evil_column": "data"})

    def test_valid_column_executes_sql(self):
        from odoo.addons.aurora.models.artifact_collector import update_instance
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        update_instance(conn, 42, {"status": "done"})
        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        self.assertIn("status = %s", sql)
        self.assertIn("WHERE id = %s", sql)

    def test_commits_on_success(self):
        from odoo.addons.aurora.models.artifact_collector import update_instance
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        update_instance(conn, 1, {"resolved": True})
        conn.commit.assert_called()

    def test_multiple_columns(self):
        from odoo.addons.aurora.models.artifact_collector import update_instance
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        update_instance(conn, 1, {"status": "done", "resolved": True, "f2p_count": 5})
        sql = cursor.execute.call_args[0][0]
        self.assertIn("status = %s", sql)
        self.assertIn("resolved = %s", sql)
        self.assertIn("f2p_count = %s", sql)


class TestEnsureInstance(TestCase):

    def test_existing_record_returns_id(self):
        from odoo.addons.aurora.models.artifact_collector import ensure_instance
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (99,)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = ensure_instance(conn, 1, "org", "repo", "inst-1")
        self.assertEqual(result, 99)

    def test_new_record_inserts_and_returns_id(self):
        from odoo.addons.aurora.models.artifact_collector import ensure_instance
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, (200,)]
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = ensure_instance(conn, 1, "org", "repo", "inst-1")
        self.assertEqual(result, 200)

    def test_passes_optional_fields_on_update(self):
        from odoo.addons.aurora.models.artifact_collector import ensure_instance
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (10,)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        ensure_instance(conn, 1, "o", "r", "i", tag_start="v1.0", tag_end="v2.0")
        conn.commit.assert_called()


class TestLoadS3Config(TestCase):

    @patch.dict(os.environ, {"AURORA_S3_ACCESS_KEY": "ak", "AURORA_S3_SECRET_KEY": "sk"})
    @patch("odoo.addons.aurora.models.pipeline.S3_BUCKET", "bucket")
    @patch("odoo.addons.aurora.models.pipeline.S3_REGION", "us-east-1")
    @patch("odoo.addons.aurora.models.pipeline.S3_AURORA_PREFIX", "aurora")
    def test_returns_config_dict(self):
        from odoo.addons.aurora.models.artifact_collector import load_s3_config
        cfg = load_s3_config()
        self.assertEqual(cfg["bucket"], "bucket")
        self.assertEqual(cfg["region"], "us-east-1")
        self.assertEqual(cfg["access_key"], "ak")
        self.assertEqual(cfg["secret_key"], "sk")
        self.assertEqual(cfg["folder"], "aurora")


class TestResolveRunNumbers(TestCase):

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_returns_dict_of_run_numbers(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import resolve_run_numbers
        mock_s3.get_next_run_number.return_value = 3
        inst = MagicMock()
        inst.pr.org = "org"
        inst.pr.repo = "repo"
        result = resolve_run_numbers({"bucket": "b"}, True, "f", "p", [inst])
        self.assertEqual(result[("org", "repo")], 3)

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_no_s3_returns_1(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import resolve_run_numbers
        inst = MagicMock()
        inst.pr.org = "org"
        inst.pr.repo = "repo"
        result = resolve_run_numbers({"bucket": "b"}, False, "f", "p", [inst])
        self.assertEqual(result[("org", "repo")], 1)

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_deduplicates_by_org_repo(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import resolve_run_numbers
        mock_s3.get_next_run_number.return_value = 2
        inst1 = MagicMock()
        inst1.pr.org = "org"
        inst1.pr.repo = "repo"
        inst2 = MagicMock()
        inst2.pr.org = "org"
        inst2.pr.repo = "repo"
        result = resolve_run_numbers({"bucket": "b"}, True, "f", "p", [inst1, inst2])
        mock_s3.get_next_run_number.assert_called_once()

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_fallback_on_exception(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import resolve_run_numbers
        mock_s3.get_next_run_number.side_effect = Exception("timeout")
        inst = MagicMock()
        inst.pr.org = "org"
        inst.pr.repo = "repo"
        result = resolve_run_numbers({"bucket": "b"}, True, "f", "p", [inst])
        self.assertEqual(result[("org", "repo")], 1)

    @patch("odoo.addons.aurora.models.artifact_collector.s3_storage")
    def test_multiple_repos(self, mock_s3):
        from odoo.addons.aurora.models.artifact_collector import resolve_run_numbers
        mock_s3.get_next_run_number.side_effect = [2, 5]
        inst1 = MagicMock()
        inst1.pr.org = "org1"
        inst1.pr.repo = "repo1"
        inst2 = MagicMock()
        inst2.pr.org = "org2"
        inst2.pr.repo = "repo2"
        result = resolve_run_numbers({"bucket": "b"}, True, "f", "p", [inst1, inst2])
        self.assertEqual(result[("org1", "repo1")], 2)
        self.assertEqual(result[("org2", "repo2")], 5)


class TestInlineCaps(TestCase):

    def test_dockerfile_cap_16kb(self):
        from odoo.addons.aurora.models.artifact_collector import _INLINE_DOCKERFILE_CAP
        self.assertEqual(_INLINE_DOCKERFILE_CAP, 16 * 1024)

    def test_report_cap_128kb(self):
        from odoo.addons.aurora.models.artifact_collector import _INLINE_REPORT_CAP
        self.assertEqual(_INLINE_REPORT_CAP, 128 * 1024)

    def test_fix_patch_cap_256kb(self):
        from odoo.addons.aurora.models.artifact_collector import _INLINE_FIX_PATCH_CAP
        self.assertEqual(_INLINE_FIX_PATCH_CAP, 256 * 1024)

    def test_log_tail_bytes_64kb(self):
        from odoo.addons.aurora.models.artifact_collector import _LOG_TAIL_BYTES
        self.assertEqual(_LOG_TAIL_BYTES, 64 * 1024)

    def test_serialization_retries_3(self):
        from odoo.addons.aurora.models.artifact_collector import _SERIALIZATION_RETRIES
        self.assertEqual(_SERIALIZATION_RETRIES, 3)
