# -*- coding: utf-8 -*-
import os
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, call


class TestIsRemote(TestCase):

    def test_https_url(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("https://bucket.s3.amazonaws.com/key"))

    def test_http_url(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("http://example.com/file.jsonl"))

    def test_s3_url(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertTrue(is_remote("s3://bucket/key/file.jsonl"))

    def test_local_path(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("/tmp/data.jsonl"))

    def test_relative_path(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("data/file.jsonl"))

    def test_none(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote(None))

    def test_empty_string(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote(""))

    def test_file_protocol(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("file:///tmp/data.jsonl"))

    def test_ftp_not_remote(self):
        from odoo.addons.aurora.models.dataset_resolver import is_remote
        self.assertFalse(is_remote("ftp://server/file"))


class TestCacheKey(TestCase):

    def test_deterministic(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_key
        self.assertEqual(_cache_key("https://x"), _cache_key("https://x"))

    def test_different_urls_different_keys(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_key
        self.assertNotEqual(_cache_key("https://a"), _cache_key("https://b"))

    def test_returns_hex_string(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_key
        import re
        key = _cache_key("https://example.com/file.jsonl")
        self.assertRegex(key, r"^[0-9a-f]{40}$")

    def test_sha1_length(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_key
        self.assertEqual(len(_cache_key("url")), 40)


class TestCacheRoot(TestCase):

    def test_creates_directory(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_root
        with tempfile.TemporaryDirectory() as d:
            root = _cache_root(d)
            self.assertTrue(os.path.isdir(root))

    def test_path_contains_dataset_cache(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_root
        with tempfile.TemporaryDirectory() as d:
            root = _cache_root(d)
            self.assertTrue(root.endswith("dataset_cache"))

    def test_idempotent(self):
        from odoo.addons.aurora.models.dataset_resolver import _cache_root
        with tempfile.TemporaryDirectory() as d:
            r1 = _cache_root(d)
            r2 = _cache_root(d)
            self.assertEqual(r1, r2)


class TestTargetPath(TestCase):

    def test_includes_basename(self):
        from odoo.addons.aurora.models.dataset_resolver import _target_path
        with tempfile.TemporaryDirectory() as d:
            path = _target_path(d, "https://bucket.s3.amazonaws.com/data/file.jsonl")
            self.assertTrue(path.endswith("file.jsonl"))

    def test_creates_bucket_dir(self):
        from odoo.addons.aurora.models.dataset_resolver import _target_path
        with tempfile.TemporaryDirectory() as d:
            path = _target_path(d, "https://example.com/file.jsonl")
            self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_no_basename_uses_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _target_path
        with tempfile.TemporaryDirectory() as d:
            path = _target_path(d, "https://example.com/")
            self.assertTrue(path.endswith("dataset.jsonl"))

    def test_different_urls_different_paths(self):
        from odoo.addons.aurora.models.dataset_resolver import _target_path
        with tempfile.TemporaryDirectory() as d:
            p1 = _target_path(d, "https://a.com/f1.jsonl")
            p2 = _target_path(d, "https://b.com/f2.jsonl")
            self.assertNotEqual(p1, p2)


class TestResolveToLocal(TestCase):

    def test_local_path_returned_unchanged(self):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local
        result = resolve_to_local(MagicMock(), "/tmp/data.jsonl")
        self.assertEqual(result, "/tmp/data.jsonl")

    def test_empty_path_returned_unchanged(self):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local
        self.assertEqual(resolve_to_local(MagicMock(), ""), "")

    def test_none_path_returned_unchanged(self):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local
        self.assertIsNone(resolve_to_local(MagicMock(), None))

    @patch("odoo.addons.aurora.models.dataset_resolver._download_http")
    @patch("odoo.addons.aurora.models.dataset_resolver._get_output_dir", return_value="/tmp/test_aurora")
    def test_https_url_downloads(self, mock_dir, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local
        with tempfile.TemporaryDirectory() as d:
            mock_dir.return_value = d
            resolve_to_local(MagicMock(), "https://bucket.s3.amazonaws.com/file.jsonl")
            mock_dl.assert_called_once()

    @patch("odoo.addons.aurora.models.dataset_resolver._download_s3")
    @patch("odoo.addons.aurora.models.dataset_resolver._get_output_dir", return_value="/tmp/test_aurora")
    def test_s3_url_uses_s3_download(self, mock_dir, mock_dl):
        from odoo.addons.aurora.models.dataset_resolver import resolve_to_local
        with tempfile.TemporaryDirectory() as d:
            mock_dir.return_value = d
            resolve_to_local(MagicMock(), "s3://bucket/key/file.jsonl")
            mock_dl.assert_called_once()


class TestClearCache(TestCase):

    def test_specific_url_removes_bucket(self):
        from odoo.addons.aurora.models.dataset_resolver import clear_cache, _cache_key
        with tempfile.TemporaryDirectory() as d:
            cache_dir = os.path.join(d, "dataset_cache")
            bucket = os.path.join(cache_dir, _cache_key("https://x"))
            os.makedirs(bucket)
            open(os.path.join(bucket, "f.jsonl"), "w").close()
            clear_cache(MagicMock(execute=MagicMock(side_effect=Exception())), "https://x")

    def test_no_url_removes_all(self):
        from odoo.addons.aurora.models.dataset_resolver import clear_cache
        with tempfile.TemporaryDirectory() as d:
            cache_dir = os.path.join(d, "dataset_cache")
            os.makedirs(cache_dir)
            open(os.path.join(cache_dir, "file"), "w").close()
            mock_cr = MagicMock()
            mock_cr.execute = MagicMock()
            mock_cr.fetchone = MagicMock(return_value=(d,))
            clear_cache(mock_cr)

    def test_nonexistent_cache_no_error(self):
        from odoo.addons.aurora.models.dataset_resolver import clear_cache
        mock_cr = MagicMock()
        mock_cr.execute = MagicMock()
        mock_cr.fetchone = MagicMock(return_value=("/nonexistent/path",))
        clear_cache(mock_cr)


class TestGetOutputDir(TestCase):

    def test_with_env_dict(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_env = MagicMock(spec=["__getitem__", "__contains__"])
        mock_icp = MagicMock()
        mock_icp.get_param.return_value = "/custom/dir"
        mock_env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=mock_icp)))
        result = _get_output_dir(mock_env)
        self.assertEqual(result, "/custom/dir")

    def test_with_raw_cursor(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_cr = MagicMock()
        mock_cr.fetchone.return_value = ("/from/db",)
        result = _get_output_dir(mock_cr)
        self.assertEqual(result, "/from/db")

    def test_cursor_returns_none_uses_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_cr = MagicMock()
        mock_cr.fetchone.return_value = None
        result = _get_output_dir(mock_cr)
        self.assertEqual(result, "/tmp/aurora_output")

    def test_cursor_exception_uses_default(self):
        from odoo.addons.aurora.models.dataset_resolver import _get_output_dir
        mock_cr = MagicMock()
        mock_cr.execute.side_effect = Exception("db error")
        result = _get_output_dir(mock_cr)
        self.assertEqual(result, "/tmp/aurora_output")


class TestPipelineExecutorUpdatePipeline(TestCase):

    def test_empty_vals_no_op(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {})
        cr.execute.assert_not_called()

    def test_disallowed_column_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_pipeline(cr, 1, {"evil_col": "x"})

    def test_allowed_column_executes(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {"stage": "done"})
        cr.execute.assert_called_once()

    def test_multiple_allowed_columns(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {"stage": "done", "pr_count": 5})
        sql = cr.execute.call_args[0][0]
        self.assertIn("stage = %s", sql)
        self.assertIn("pr_count = %s", sql)

    def test_params_include_values_and_id(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 42, {"stage": "failed"})
        params = cr.execute.call_args[0][1]
        self.assertIn("failed", params)
        self.assertEqual(params[-1], 42)


class TestPipelineExecutorAppendLog(TestCase):

    def test_appends_with_timestamp(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "hello")
        sql = cr.execute.call_args[0][0]
        self.assertIn("aurora_pipeline", sql)
        self.assertIn("log", sql)

    def test_includes_message(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "step done")
        params = cr.execute.call_args[0][1]
        self.assertTrue(any("step done" in str(p) for p in params))

    def test_caps_at_max_size(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log, _MAX_LOG_SIZE
        cr = MagicMock()
        _append_log(cr, 1, "x")
        params = cr.execute.call_args[0][1]
        self.assertIn(_MAX_LOG_SIZE, params)


class TestPipelineExecutorAppendStepLog(TestCase):

    def test_valid_step_executes(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        cr = MagicMock()
        _append_step_log(cr, 1, 3, "progress")
        cr.execute.assert_called_once()

    def test_invalid_step_no_op(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        cr = MagicMock()
        _append_step_log(cr, 1, 99, "msg")
        cr.execute.assert_not_called()

    def test_step_1_through_6_valid(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        for step in range(1, 7):
            cr = MagicMock()
            _append_step_log(cr, 1, step, "msg")
            cr.execute.assert_called_once()


class TestPipelineExecutorHeartbeat(TestCase):

    def test_updates_last_heartbeat(self):
        from odoo.addons.aurora.models.pipeline_executor import _heartbeat
        cr = MagicMock()
        _heartbeat(cr, 1)
        cr.execute.assert_called()
        cr.commit.assert_called()

    def test_with_progress_text(self):
        from odoo.addons.aurora.models.pipeline_executor import _heartbeat
        cr = MagicMock()
        _heartbeat(cr, 1, "Step 3 running")
        sql = cr.execute.call_args[0][0]
        self.assertIn("progress_text", sql)

    def test_without_progress_text(self):
        from odoo.addons.aurora.models.pipeline_executor import _heartbeat
        cr = MagicMock()
        _heartbeat(cr, 1, None)
        cr.commit.assert_called()


class TestPipelineExecutorFailPipeline(TestCase):

    def test_sets_stage_failed(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        cr = MagicMock()
        _fail_pipeline(cr, 1, "step2_status", "error msg")
        calls = cr.execute.call_args_list
        sql_combined = " ".join(str(c) for c in calls)
        self.assertIn("failed", sql_combined)

    def test_appends_to_log(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        cr = MagicMock()
        _fail_pipeline(cr, 1, "step1_status", Exception("boom"))
        self.assertTrue(cr.execute.call_count >= 2)


class TestCountJsonlLines(TestCase):

    def test_empty_file(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            self.assertEqual(_count_jsonl_lines(path), 0)
        finally:
            os.unlink(path)

    def test_multiple_lines(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a":1}\n{"b":2}\n{"c":3}\n')
            path = f.name
        try:
            self.assertEqual(_count_jsonl_lines(path), 3)
        finally:
            os.unlink(path)

    def test_none_path(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        self.assertEqual(_count_jsonl_lines(None), 0)

    def test_nonexistent_path(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        self.assertEqual(_count_jsonl_lines("/no/such/file.jsonl"), 0)

    def test_single_line(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"x":1}\n')
            path = f.name
        try:
            self.assertEqual(_count_jsonl_lines(path), 1)
        finally:
            os.unlink(path)


class TestValidateStepOutput(TestCase):

    def test_none_path_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            _validate_step_output(None, 1)

    def test_nonexistent_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            _validate_step_output("/no/file.jsonl", 2)

    def test_empty_file_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            with self.assertRaises(AuroraPipelineError):
                _validate_step_output(path, 3)
        finally:
            os.unlink(path)

    def test_valid_jsonl_passes(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"valid": true}\n')
            path = f.name
        try:
            _validate_step_output(path, 1)
        finally:
            os.unlink(path)

    def test_invalid_json_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("not json at all\n")
            path = f.name
        try:
            with self.assertRaises(AuroraPipelineError):
                _validate_step_output(path, 4)
        finally:
            os.unlink(path)


class TestRequestCancel(TestCase):

    def test_cancel_existing_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            request_cancel, _register_cancel_event, _unregister_cancel_event,
        )
        event = _register_cancel_event(9999)
        try:
            self.assertTrue(request_cancel(9999))
            self.assertTrue(event.is_set())
        finally:
            _unregister_cancel_event(9999)

    def test_cancel_nonexistent_returns_false(self):
        from odoo.addons.aurora.models.pipeline_executor import request_cancel
        self.assertFalse(request_cancel(88888))

    def test_register_creates_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            _register_cancel_event, _unregister_cancel_event,
        )
        event = _register_cancel_event(7777)
        try:
            self.assertIsInstance(event, threading.Event)
            self.assertFalse(event.is_set())
        finally:
            _unregister_cancel_event(7777)

    def test_unregister_removes_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            _register_cancel_event, _unregister_cancel_event, request_cancel,
        )
        _register_cancel_event(6666)
        _unregister_cancel_event(6666)
        self.assertFalse(request_cancel(6666))


class TestCheckCancelled(TestCase):

    def test_no_event_does_nothing(self):
        from odoo.addons.aurora.models.pipeline_executor import check_cancelled
        check_cancelled()

    def test_event_not_set_does_nothing(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            check_cancelled, _register_thread_cancel, _unregister_thread_cancel,
        )
        event = threading.Event()
        _register_thread_cancel(event)
        try:
            check_cancelled()
        finally:
            _unregister_thread_cancel()

    def test_event_set_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            check_cancelled, _register_thread_cancel, _unregister_thread_cancel,
            PipelineCancelled,
        )
        event = threading.Event()
        event.set()
        _register_thread_cancel(event)
        try:
            with self.assertRaises(PipelineCancelled):
                check_cancelled()
        finally:
            _unregister_thread_cancel()


class TestCancellableSleep(TestCase):

    def test_no_event_sleeps_normally(self):
        from odoo.addons.aurora.models.pipeline_executor import cancellable_sleep
        import time
        t0 = time.time()
        cancellable_sleep(0.01)
        self.assertGreaterEqual(time.time() - t0, 0.009)

    def test_event_set_raises_immediately(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            cancellable_sleep, _register_thread_cancel, _unregister_thread_cancel,
            PipelineCancelled,
        )
        event = threading.Event()
        event.set()
        _register_thread_cancel(event)
        try:
            with self.assertRaises(PipelineCancelled):
                cancellable_sleep(10)
        finally:
            _unregister_thread_cancel()


class TestAllowedColumns(TestCase):

    def test_stage_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("stage", _ALLOWED_COLUMNS)

    def test_step_statuses_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        for i in range(1, 7):
            self.assertIn(f"step{i}_status", _ALLOWED_COLUMNS)

    def test_step_files_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        for i in range(1, 7):
            self.assertIn(f"step{i}_file", _ALLOWED_COLUMNS)

    def test_step_logs_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        for i in range(1, 7):
            self.assertIn(f"step{i}_log", _ALLOWED_COLUMNS)

    def test_phase_statuses_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase1_status", _ALLOWED_COLUMNS)
        self.assertIn("phase2_status", _ALLOWED_COLUMNS)
        self.assertIn("phase3_status", _ALLOWED_COLUMNS)

    def test_id_not_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertNotIn("id", _ALLOWED_COLUMNS)

    def test_job_name_not_allowed(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertNotIn("job_name", _ALLOWED_COLUMNS)
