# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock


class TestPipelineRequestCancel(unittest.TestCase):
    """Tests for pipeline_executor.request_cancel"""

    def test_request_cancel_returns_true_when_event_exists(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            request_cancel, _cancel_events, _cancel_lock,
        )
        event = threading.Event()
        with _cancel_lock:
            _cancel_events[999] = event
        try:
            self.assertTrue(request_cancel(999))
            self.assertTrue(event.is_set())
        finally:
            with _cancel_lock:
                _cancel_events.pop(999, None)

    def test_request_cancel_returns_false_when_no_event(self):
        from odoo.addons.aurora.models.pipeline_executor import request_cancel
        result = request_cancel(88888)
        self.assertFalse(result)


class TestPipelineCancelEventLifecycle(unittest.TestCase):
    """Tests for _register_cancel_event / _unregister_cancel_event"""

    def test_register_cancel_event_creates_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            _register_cancel_event, _cancel_events, _cancel_lock,
        )
        event = _register_cancel_event(1001)
        try:
            self.assertIsInstance(event, threading.Event)
            with _cancel_lock:
                self.assertIn(1001, _cancel_events)
        finally:
            with _cancel_lock:
                _cancel_events.pop(1001, None)

    def test_unregister_cancel_event_removes_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            _register_cancel_event, _unregister_cancel_event,
            _cancel_events, _cancel_lock,
        )
        _register_cancel_event(1002)
        _unregister_cancel_event(1002)
        with _cancel_lock:
            self.assertNotIn(1002, _cancel_events)

    def test_unregister_nonexistent_does_not_raise(self):
        from odoo.addons.aurora.models.pipeline_executor import _unregister_cancel_event
        _unregister_cancel_event(99999)
        self.assertTrue(True)


class TestPipelineAllowedColumns(unittest.TestCase):
    """Tests for _ALLOWED_COLUMNS frozenset"""

    def test_contains_step1_status(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("step1_status", _ALLOWED_COLUMNS)

    def test_contains_stage(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("stage", _ALLOWED_COLUMNS)

    def test_contains_last_heartbeat(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("last_heartbeat", _ALLOWED_COLUMNS)

    def test_contains_progress_text(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("progress_text", _ALLOWED_COLUMNS)

    def test_rejects_invalid_column(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertNotIn("evil_column", _ALLOWED_COLUMNS)

    def test_is_frozenset(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIsInstance(_ALLOWED_COLUMNS, frozenset)


class TestUpdatePipeline(unittest.TestCase):
    """Tests for _update_pipeline"""

    def test_calls_cr_execute_with_valid_columns(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {"stage": "running"})
        cr.execute.assert_called_once()

    def test_rejects_invalid_columns_with_value_error(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_pipeline(cr, 1, {"bad_col": "x"})

    def test_does_nothing_when_vals_empty(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {})
        cr.execute.assert_not_called()

    @patch("odoo.addons.aurora.models.pipeline_executor.time.sleep")
    def test_serialization_retry_logic(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        cr.execute.side_effect = [
            psycopg2.errors.SerializationFailure(),
            None,
        ]
        _update_pipeline(cr, 1, {"stage": "done"})
        self.assertEqual(cr.execute.call_count, 2)
        cr.rollback.assert_called_once()

    @patch("odoo.addons.aurora.models.pipeline_executor.time.sleep")
    def test_serialization_exhausted_raises(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        cr.execute.side_effect = psycopg2.errors.SerializationFailure()
        with self.assertRaises(psycopg2.errors.SerializationFailure):
            _update_pipeline(cr, 1, {"stage": "done"})


class TestAppendLog(unittest.TestCase):
    """Tests for pipeline _append_log"""

    def test_append_log_calls_execute(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "hello")
        cr.execute.assert_called_once()

    def test_append_log_timestamp_format(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "test msg")
        args = cr.execute.call_args[0][1]
        line = args[0]
        self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_append_log_max_log_size_cap(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log, _MAX_LOG_SIZE
        cr = MagicMock()
        _append_log(cr, 1, "msg")
        args = cr.execute.call_args[0][1]
        self.assertEqual(args[1], _MAX_LOG_SIZE)


class TestAppendStepLog(unittest.TestCase):
    """Tests for _append_step_log"""

    def test_valid_step_calls_execute(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        cr = MagicMock()
        _append_step_log(cr, 1, 1, "step msg")
        cr.execute.assert_called_once()

    def test_invalid_step_does_not_call_execute(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        cr = MagicMock()
        _append_step_log(cr, 1, 99, "msg")
        cr.execute.assert_not_called()

    def test_timestamp_format_hms(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_step_log
        cr = MagicMock()
        _append_step_log(cr, 1, 2, "x")
        args = cr.execute.call_args[0][1]
        line = args[0]
        self.assertRegex(line, r"^\[\d{2}:\d{2}:\d{2}\]")


class TestHeartbeat(unittest.TestCase):
    """Tests for pipeline _heartbeat"""

    def test_heartbeat_calls_update_pipeline_and_commit(self):
        from odoo.addons.aurora.models.pipeline_executor import _heartbeat
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.pipeline_executor._update_pipeline") as mock_up:
            _heartbeat(cr, 1)
            mock_up.assert_called_once()
            cr.commit.assert_called_once()

    def test_heartbeat_with_progress_text(self):
        from odoo.addons.aurora.models.pipeline_executor import _heartbeat
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.pipeline_executor._update_pipeline") as mock_up:
            _heartbeat(cr, 1, progress_text="doing stuff")
            vals = mock_up.call_args[0][2]
            self.assertIn("progress_text", vals)
            self.assertEqual(vals["progress_text"], "doing stuff")


class TestFailPipeline(unittest.TestCase):
    """Tests for _fail_pipeline"""

    def test_sets_stage_failed(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.pipeline_executor._update_pipeline") as mock_up:
            with patch("odoo.addons.aurora.models.pipeline_executor._append_log"):
                _fail_pipeline(cr, 1, "step2_status", "boom")
                vals = mock_up.call_args[0][2]
                self.assertEqual(vals["stage"], "failed")
                self.assertEqual(vals["step2_status"], "failed")

    def test_appends_log_with_exc(self):
        from odoo.addons.aurora.models.pipeline_executor import _fail_pipeline
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.pipeline_executor._update_pipeline"):
            with patch("odoo.addons.aurora.models.pipeline_executor._append_log") as mock_log:
                _fail_pipeline(cr, 1, "step3_status", "error msg")
                log_msg = mock_log.call_args[0][2]
                self.assertIn("error msg", log_msg)


class TestDbLogStream(unittest.TestCase):
    """Tests for _DbLogStream"""

    def test_write_returns_length(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        result = stream.write("hello")
        self.assertEqual(result, 5)

    def test_write_empty_returns_zero(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        result = stream.write("")
        self.assertEqual(result, 0)

    def test_cr_handling_on_write(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=0)
        with patch("odoo.addons.aurora.models.pipeline_executor._open_cursor") as mock_oc:
            mock_cr = MagicMock()
            mock_oc.return_value = mock_cr
            stream.write("line\n")
            mock_oc.assert_called_once_with("testdb")

    def test_carriage_return_handling(self):
        from datetime import datetime
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        stream._last_flush = datetime.now().timestamp()
        stream.write("progress\r")
        self.assertEqual(stream._current_line, "")
        self.assertIn("progress\n", stream._buffer)

    def test_flush_calls_drain(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        stream._buffer.append("stuff\n")
        with patch("odoo.addons.aurora.models.pipeline_executor._open_cursor") as mock_oc:
            mock_cr = MagicMock()
            mock_oc.return_value = mock_cr
            stream.flush()
            mock_cr.execute.assert_called_once()

    def test_final_flush_closes_cursor(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        mock_cr = MagicMock()
        stream._cr = mock_cr
        stream.final_flush()
        mock_cr.close.assert_called_once()
        self.assertIsNone(stream._cr)

    def test_drain_no_buffer_does_nothing(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("testdb", 1, None, flush_interval=999)
        stream._drain()
        self.assertIsNone(stream._cr)


class TestPostChatter(unittest.TestCase):
    """Tests for pipeline _post_chatter"""

    @patch("odoo.addons.aurora.models.pipeline_executor._open_cursor")
    def test_opens_cursor_and_posts(self, mock_oc):
        from odoo.addons.aurora.models.pipeline_executor import _post_chatter
        mock_cr = MagicMock()
        mock_oc.return_value = mock_cr
        mock_env = MagicMock()
        mock_rec = MagicMock()
        mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=mock_rec)))
        with patch("odoo.api.Environment", return_value=mock_env):
            with patch("odoo.SUPERUSER_ID", 1):
                _post_chatter("testdb", 2, 10, "hello")
        mock_cr.close.assert_called()


class TestNotifyBusPipeline(unittest.TestCase):
    """Tests for pipeline _notify_bus"""

    def test_returns_none(self):
        from odoo.addons.aurora.models.pipeline_executor import _notify_bus
        result = _notify_bus("db", 1, "done", "text")
        self.assertIsNone(result)


class TestSafeWorkerPipeline(unittest.TestCase):
    """Tests for pipeline _safe_worker decorator"""

    def test_releases_semaphore_on_success(self):
        from odoo.addons.aurora.models.pipeline_executor import _safe_worker, _semaphore
        _semaphore.acquire(blocking=False)
        @_safe_worker
        def good_fn():
            return "ok"
        good_fn()
        acquired = _semaphore.acquire(blocking=False)
        self.assertTrue(acquired)
        _semaphore.release()

    def test_releases_semaphore_on_failure(self):
        from odoo.addons.aurora.models.pipeline_executor import _safe_worker, _semaphore
        _semaphore.acquire(blocking=False)
        @_safe_worker
        def bad_fn():
            raise RuntimeError("boom")
        with patch("odoo.addons.aurora.models.pipeline_executor._open_cursor"):
            bad_fn()
        acquired = _semaphore.acquire(blocking=False)
        self.assertTrue(acquired)
        _semaphore.release()


class TestReadConfig(unittest.TestCase):
    """Tests for _read_config"""

    @patch("odoo.addons.aurora.models.pipeline_executor._open_cursor")
    def test_returns_expected_keys(self, mock_oc):
        from odoo.addons.aurora.models.pipeline_executor import _read_config
        mock_cr = MagicMock()
        mock_oc.return_value = mock_cr
        mock_pipeline = MagicMock()
        mock_pipeline.exists.return_value = True
        mock_pipeline.github_org = "org"
        mock_pipeline.github_repo = "repo"
        mock_pipeline.output_dir = "/tmp/out"
        mock_pipeline.skip_pr_fetch = False
        mock_pipeline.detected_lang = "python"
        mock_env = MagicMock()
        mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=mock_pipeline)))
        mock_icp = MagicMock()
        mock_icp.get_param = MagicMock(return_value="100")
        mock_env_sudo = MagicMock()
        mock_env_sudo.sudo.return_value = mock_icp
        with patch("odoo.api.Environment", return_value=mock_env):
            with patch("odoo.SUPERUSER_ID", 1):
                with patch("odoo.addons.aurora.models.pipeline.S3_BUCKET", "bkt"):
                    with patch("odoo.addons.aurora.models.pipeline.S3_REGION", "us-east-1"):
                        with patch("odoo.addons.aurora.models.pipeline.S3_AURORA_PREFIX", "pfx"):
                            with patch("odoo.addons.aurora.models.pipeline._get_env", return_value="key"):
                                result = _read_config("db", 1)
        self.assertIn("org", result)
        self.assertIn("repo", result)
        self.assertIn("s3_bucket", result)


class TestCountJsonlLines(unittest.TestCase):
    """Tests for _count_jsonl_lines"""

    def test_returns_zero_for_empty_path(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        self.assertEqual(_count_jsonl_lines(None), 0)

    def test_returns_zero_for_missing_file(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        self.assertEqual(_count_jsonl_lines("/nonexistent/file.jsonl"), 0)

    def test_counts_lines_correctly(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a":1}\n{"b":2}\n{"c":3}\n')
            path = f.name
        try:
            self.assertEqual(_count_jsonl_lines(path), 3)
        finally:
            os.unlink(path)


class TestValidateStepOutput(unittest.TestCase):
    """Tests for _validate_step_output"""

    def test_missing_file_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        with self.assertRaises(Exception) as ctx:
            _validate_step_output("/nonexistent/x.jsonl", 1)
        self.assertIn("missing", str(ctx.exception).lower())

    def test_empty_file_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            with self.assertRaises(Exception) as ctx:
                _validate_step_output(path, 2)
            self.assertIn("empty", str(ctx.exception).lower())
        finally:
            os.unlink(path)

    def test_invalid_json_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("not json at all\n")
            path = f.name
        try:
            with self.assertRaises(Exception) as ctx:
                _validate_step_output(path, 3)
            self.assertIn("invalid", str(ctx.exception).lower())
        finally:
            os.unlink(path)

    def test_valid_jsonl_does_not_raise(self):
        from odoo.addons.aurora.models.pipeline_executor import _validate_step_output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"ok": true}\n')
            path = f.name
        try:
            _validate_step_output(path, 1)
        finally:
            os.unlink(path)


class TestPipelineCancelledException(unittest.TestCase):
    """Tests for PipelineCancelled"""

    def test_is_exception(self):
        from odoo.addons.aurora.models.pipeline_executor import PipelineCancelled
        self.assertTrue(issubclass(PipelineCancelled, Exception))

    def test_can_be_raised_with_message(self):
        from odoo.addons.aurora.models.pipeline_executor import PipelineCancelled
        with self.assertRaises(PipelineCancelled) as ctx:
            raise PipelineCancelled("cancel!")
        self.assertEqual(str(ctx.exception), "cancel!")


class TestCheckCancelled(unittest.TestCase):
    """Tests for check_cancelled"""

    def test_raises_when_event_set(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            check_cancelled, PipelineCancelled,
            _thread_cancel_events, _thread_cancel_lock,
        )
        event = threading.Event()
        event.set()
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events[tid] = event
        try:
            with self.assertRaises(PipelineCancelled):
                check_cancelled()
        finally:
            with _thread_cancel_lock:
                _thread_cancel_events.pop(tid, None)

    def test_does_not_raise_when_event_not_set(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            check_cancelled, _thread_cancel_events, _thread_cancel_lock,
        )
        event = threading.Event()
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events[tid] = event
        try:
            check_cancelled()
            self.assertTrue(True)
        finally:
            with _thread_cancel_lock:
                _thread_cancel_events.pop(tid, None)

    def test_does_not_raise_when_no_event_registered(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            check_cancelled, _thread_cancel_events, _thread_cancel_lock,
        )
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events.pop(tid, None)
        check_cancelled()
        self.assertTrue(True)


class TestCancellableSleep(unittest.TestCase):
    """Tests for cancellable_sleep"""

    def test_returns_early_when_cancelled(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            cancellable_sleep, PipelineCancelled,
            _thread_cancel_events, _thread_cancel_lock,
        )
        event = threading.Event()
        event.set()
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events[tid] = event
        try:
            with self.assertRaises(PipelineCancelled):
                cancellable_sleep(10.0)
        finally:
            with _thread_cancel_lock:
                _thread_cancel_events.pop(tid, None)

    def test_sleeps_when_no_event(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            cancellable_sleep, _thread_cancel_events, _thread_cancel_lock,
        )
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events.pop(tid, None)
        start = time.time()
        cancellable_sleep(0.05)
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, 0.04)


class TestSubmitPipelineAsync(unittest.TestCase):
    """Tests for submit_pipeline_async"""

    @patch("odoo.addons.aurora.models.pipeline_executor._executor")
    @patch("odoo.addons.aurora.models.pipeline_executor._semaphore")
    def test_acquires_semaphore_returns_true(self, mock_sem, mock_exec):
        from odoo.addons.aurora.models.pipeline_executor import submit_pipeline_async
        mock_sem.acquire.return_value = True
        result = submit_pipeline_async("db", 1, 10)
        self.assertTrue(result)
        mock_exec.submit.assert_called_once()

    @patch("odoo.addons.aurora.models.pipeline_executor._executor")
    @patch("odoo.addons.aurora.models.pipeline_executor._semaphore")
    def test_returns_false_when_semaphore_full(self, mock_sem, mock_exec):
        from odoo.addons.aurora.models.pipeline_executor import submit_pipeline_async
        mock_sem.acquire.return_value = False
        result = submit_pipeline_async("db", 1, 10)
        self.assertFalse(result)
        mock_exec.submit.assert_not_called()


class TestPipelineConstants(unittest.TestCase):
    """Tests for pipeline constants"""

    def test_max_pipeline_threads(self):
        from odoo.addons.aurora.models.pipeline_executor import _MAX_PIPELINE_THREADS
        self.assertEqual(_MAX_PIPELINE_THREADS, 2)

    def test_max_concurrent_pipelines(self):
        from odoo.addons.aurora.models.pipeline_executor import _MAX_CONCURRENT_PIPELINES
        self.assertEqual(_MAX_CONCURRENT_PIPELINES, 2)

    def test_heartbeat_stale_seconds(self):
        from odoo.addons.aurora.models.pipeline_executor import _HEARTBEAT_STALE_SECONDS
        self.assertEqual(_HEARTBEAT_STALE_SECONDS, 120)

    def test_serialization_retries(self):
        from odoo.addons.aurora.models.pipeline_executor import _SERIALIZATION_RETRIES
        self.assertEqual(_SERIALIZATION_RETRIES, 3)

    def test_serialization_backoff(self):
        from odoo.addons.aurora.models.pipeline_executor import _SERIALIZATION_BACKOFF
        self.assertEqual(_SERIALIZATION_BACKOFF, 0.5)

    def test_max_log_size(self):
        from odoo.addons.aurora.models.pipeline_executor import _MAX_LOG_SIZE
        self.assertEqual(_MAX_LOG_SIZE, 2_000_000)





class TestEvalRequestCancel(unittest.TestCase):
    """Tests for evaluation_executor.request_cancel"""

    def test_returns_true_when_event_exists(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            request_cancel, _cancel_events, _cancel_lock,
        )
        event = threading.Event()
        with _cancel_lock:
            _cancel_events[2001] = event
        try:
            result = request_cancel(2001)
            self.assertTrue(result)
            self.assertTrue(event.is_set())
        finally:
            with _cancel_lock:
                _cancel_events.pop(2001, None)

    def test_returns_false_when_no_event(self):
        from odoo.addons.aurora.models.evaluation_executor import request_cancel
        result = request_cancel(77777)
        self.assertFalse(result)


class TestEvalCancelEventLifecycle(unittest.TestCase):
    """Tests for eval _register_cancel_event / _unregister_cancel_event"""

    def test_register_creates_event(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            _register_cancel_event, _cancel_events, _cancel_lock,
        )
        event = _register_cancel_event(3001)
        try:
            self.assertIsInstance(event, threading.Event)
            with _cancel_lock:
                self.assertIn(3001, _cancel_events)
        finally:
            with _cancel_lock:
                _cancel_events.pop(3001, None)

    def test_unregister_removes_event(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            _register_cancel_event, _unregister_cancel_event,
            _cancel_events, _cancel_lock,
        )
        _register_cancel_event(3002)
        _unregister_cancel_event(3002)
        with _cancel_lock:
            self.assertNotIn(3002, _cancel_events)


class TestEvalAllowedColumns(unittest.TestCase):
    """Tests for eval _ALLOWED_COLUMNS"""

    def test_contains_stage(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("stage", _ALLOWED_COLUMNS)

    def test_contains_build_status(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("build_status", _ALLOWED_COLUMNS)

    def test_contains_run_status(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("run_status", _ALLOWED_COLUMNS)

    def test_contains_total_instances(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("total_instances", _ALLOWED_COLUMNS)

    def test_rejects_invalid_column(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertNotIn("hack_column", _ALLOWED_COLUMNS)


class TestEvalAllowedInstanceColumns(unittest.TestCase):
    """Tests for _ALLOWED_INSTANCE_COLUMNS"""

    def test_contains_status(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("status", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_resolved(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("resolved", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_dockerfile_content(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("dockerfile_content", _ALLOWED_INSTANCE_COLUMNS)

    def test_rejects_invalid_column(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertNotIn("malicious", _ALLOWED_INSTANCE_COLUMNS)


class TestUpdateEval(unittest.TestCase):
    """Tests for _update_eval"""

    def test_calls_execute_with_valid_columns(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        _update_eval(cr, 1, {"stage": "done"})
        cr.execute.assert_called_once()

    def test_rejects_invalid_columns(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_eval(cr, 1, {"nope_col": "val"})

    def test_does_nothing_when_empty(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        _update_eval(cr, 1, {})
        cr.execute.assert_not_called()

    @patch("odoo.addons.aurora.models.evaluation_executor.time.sleep")
    def test_serialization_retry(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        cr.execute.side_effect = [
            psycopg2.errors.SerializationFailure(),
            None,
        ]
        _update_eval(cr, 1, {"stage": "done"})
        self.assertEqual(cr.execute.call_count, 2)


class TestEvalAppendLog(unittest.TestCase):
    """Tests for eval _append_log"""

    def test_calls_execute(self):
        from odoo.addons.aurora.models.evaluation_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "log msg")
        cr.execute.assert_called_once()

    def test_timestamp_format(self):
        from odoo.addons.aurora.models.evaluation_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "hi")
        args = cr.execute.call_args[0][1]
        self.assertRegex(args[0], r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    @patch("odoo.addons.aurora.models.evaluation_executor.time.sleep")
    def test_retry_on_serialization_failure(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.evaluation_executor import _append_log
        cr = MagicMock()
        cr.execute.side_effect = [
            psycopg2.errors.SerializationFailure(),
            None,
        ]
        _append_log(cr, 1, "msg")
        self.assertEqual(cr.execute.call_count, 2)


class TestEvalHeartbeat(unittest.TestCase):
    """Tests for eval _heartbeat"""

    def test_calls_update_eval_and_commit(self):
        from odoo.addons.aurora.models.evaluation_executor import _heartbeat
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.evaluation_executor._update_eval") as mock_up:
            _heartbeat(cr, 1)
            mock_up.assert_called_once()
            cr.commit.assert_called_once()

    def test_progress_text_optional(self):
        from odoo.addons.aurora.models.evaluation_executor import _heartbeat
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.evaluation_executor._update_eval") as mock_up:
            _heartbeat(cr, 1, progress_text="50%")
            vals = mock_up.call_args[0][2]
            self.assertEqual(vals["progress_text"], "50%")

    def test_no_progress_text(self):
        from odoo.addons.aurora.models.evaluation_executor import _heartbeat
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.evaluation_executor._update_eval") as mock_up:
            _heartbeat(cr, 1)
            vals = mock_up.call_args[0][2]
            self.assertNotIn("progress_text", vals)


class TestFailEval(unittest.TestCase):
    """Tests for _fail_eval"""

    def test_sets_stage_and_step_failed(self):
        from odoo.addons.aurora.models.evaluation_executor import _fail_eval
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.evaluation_executor._update_eval") as mock_up:
            with patch("odoo.addons.aurora.models.evaluation_executor._append_log"):
                _fail_eval(cr, 1, "build_status", "err")
                vals = mock_up.call_args[0][2]
                self.assertEqual(vals["stage"], "failed")
                self.assertEqual(vals["build_status"], "failed")

    def test_appends_log_with_exc_info(self):
        from odoo.addons.aurora.models.evaluation_executor import _fail_eval
        cr = MagicMock()
        with patch("odoo.addons.aurora.models.evaluation_executor._update_eval"):
            with patch("odoo.addons.aurora.models.evaluation_executor._append_log") as mock_log:
                _fail_eval(cr, 1, "run_status", "crashed")
                msg = mock_log.call_args[0][2]
                self.assertIn("crashed", msg)


class TestEvalNotifyBus(unittest.TestCase):
    """Tests for eval _notify_bus"""

    def test_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _notify_bus
        result = _notify_bus("db", 1)
        self.assertIsNone(result)


class TestRegisterLhtMetadata(unittest.TestCase):
    """Tests for register_lht_metadata / _lookup_lht"""

    def test_register_and_lookup(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            register_lht_metadata, _lookup_lht, _lht_metadata_cache, _lht_metadata_lock,
        )
        register_lht_metadata("org1", "repo1", 42, {"instance_id": "org1__repo1-pr-42"})
        try:
            result = _lookup_lht("org1", "repo1", 42)
            self.assertEqual(result["instance_id"], "org1__repo1-pr-42")
        finally:
            with _lht_metadata_lock:
                _lht_metadata_cache.pop(("org1", "repo1", 42), None)

    def test_lookup_missing_returns_empty(self):
        from odoo.addons.aurora.models.evaluation_executor import _lookup_lht
        result = _lookup_lht("no", "exist", 0)
        self.assertEqual(result, {})

    def test_register_overwrites(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            register_lht_metadata, _lookup_lht, _lht_metadata_cache, _lht_metadata_lock,
        )
        register_lht_metadata("o", "r", 1, {"instance_id": "a"})
        register_lht_metadata("o", "r", 1, {"instance_id": "b"})
        try:
            result = _lookup_lht("o", "r", 1)
            self.assertEqual(result["instance_id"], "b")
        finally:
            with _lht_metadata_lock:
                _lht_metadata_cache.pop(("o", "r", 1), None)


class TestInstanceIdFor(unittest.TestCase):
    """Tests for _instance_id_for"""

    def test_with_lht_metadata(self):
        from odoo.addons.aurora.models.evaluation_executor import (
            _instance_id_for, register_lht_metadata,
            _lht_metadata_cache, _lht_metadata_lock,
        )
        register_lht_metadata("myorg", "myrepo", 5, {"instance_id": "custom-id"})
        pr = MagicMock()
        pr.org = "myorg"
        pr.repo = "myrepo"
        pr.number = 5
        try:
            result = _instance_id_for(pr)
            self.assertEqual(result, "custom-id")
        finally:
            with _lht_metadata_lock:
                _lht_metadata_cache.pop(("myorg", "myrepo", 5), None)

    def test_without_lht_metadata_fallback(self):
        from odoo.addons.aurora.models.evaluation_executor import _instance_id_for
        pr = MagicMock()
        pr.org = "fallorg"
        pr.repo = "fallrepo"
        pr.number = 99
        result = _instance_id_for(pr)
        self.assertEqual(result, "fallorg__fallrepo-pr-99")


class TestReadTail(unittest.TestCase):
    """Tests for _read_tail"""

    def test_missing_file_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_tail
        result = _read_tail("/nonexistent/file.log")
        self.assertIsNone(result)

    def test_none_path_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_tail
        result = _read_tail(None)
        self.assertIsNone(result)

    def test_large_file_truncation(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_tail
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b"A" * 200)
            path = f.name
        try:
            result = _read_tail(path, max_bytes=50)
            self.assertEqual(len(result), 50)
        finally:
            os.unlink(path)

    def test_small_file_full_read(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_tail
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b"short content")
            path = f.name
        try:
            result = _read_tail(path, max_bytes=1024)
            self.assertEqual(result, "short content")
        finally:
            os.unlink(path)


class TestReadCapped(unittest.TestCase):
    """Tests for _read_capped"""

    def test_missing_file_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_capped
        result = _read_capped("/nonexistent/file.txt", 100)
        self.assertIsNone(result)

    def test_none_path_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_capped
        result = _read_capped(None, 100)
        self.assertIsNone(result)

    def test_truncation_at_cap(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_capped
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b"X" * 200)
            path = f.name
        try:
            result = _read_capped(path, 50)
            self.assertIn("[truncated]", result)
        finally:
            os.unlink(path)

    def test_under_cap_no_truncation(self):
        from odoo.addons.aurora.models.evaluation_executor import _read_capped
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b"hello")
            path = f.name
        try:
            result = _read_capped(path, 1000)
            self.assertEqual(result, "hello")
            self.assertNotIn("[truncated]", result)
        finally:
            os.unlink(path)


class TestUploadArtifact(unittest.TestCase):
    """Tests for _upload_artifact"""

    def test_s3_off_returns_none(self):
        from odoo.addons.aurora.models.evaluation_executor import _upload_artifact
        result = _upload_artifact({}, False, "/some/path", "key")
        self.assertIsNone(result)

    @patch("odoo.addons.aurora.models.evaluation_executor.s3_storage.upload_file")
    def test_s3_on_success(self, mock_upload):
        from odoo.addons.aurora.models.evaluation_executor import _upload_artifact
        mock_upload.return_value = "https://s3.example.com/key"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            result = _upload_artifact({"bucket": "b"}, True, path, "key")
            self.assertEqual(result, "https://s3.example.com/key")
        finally:
            os.unlink(path)

    @patch("odoo.addons.aurora.models.evaluation_executor.s3_storage.upload_file")
    def test_s3_on_failure_returns_none(self, mock_upload):
        from odoo.addons.aurora.models.evaluation_executor import _upload_artifact
        mock_upload.side_effect = Exception("s3 error")
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            result = _upload_artifact({"bucket": "b"}, True, path, "key")
            self.assertIsNone(result)
        finally:
            os.unlink(path)


class TestBuildInstanceKey(unittest.TestCase):
    """Tests for _build_instance_key"""

    @patch("odoo.addons.aurora.models.evaluation_executor.s3_storage.build_s3_key")
    def test_correct_path_format(self, mock_build):
        from odoo.addons.aurora.models.evaluation_executor import _build_instance_key
        mock_build.return_value = "aurora/phase2/org__repo/run_1/inst-1/file.log"
        result = _build_instance_key("aurora", "phase2", "org", "repo", 1, "inst-1", "file.log")
        mock_build.assert_called_once_with("org", "repo", 1, "inst-1/file.log", folder="aurora", phase="phase2")
        self.assertEqual(result, "aurora/phase2/org__repo/run_1/inst-1/file.log")


class TestEnsureInstance(unittest.TestCase):
    """Tests for _ensure_instance"""

    def test_existing_row_updates(self):
        from odoo.addons.aurora.models.evaluation_executor import _ensure_instance
        cr = MagicMock()
        cr.fetchone.return_value = (42,)
        with patch("odoo.addons.aurora.models.evaluation_executor._update_instance") as mock_ui:
            result = _ensure_instance(cr, 1, "org", "repo", "iid", tag_start="v1")
            self.assertEqual(result, 42)
            mock_ui.assert_called_once()

    def test_new_insert(self):
        from odoo.addons.aurora.models.evaluation_executor import _ensure_instance
        cr = MagicMock()
        cr.fetchone.side_effect = [None, (99,)]
        result = _ensure_instance(cr, 1, "org", "repo", "iid-new")
        self.assertEqual(result, 99)
        self.assertGreaterEqual(cr.execute.call_count, 2)


class TestUpdateInstance(unittest.TestCase):
    """Tests for _update_instance"""

    def test_rejects_invalid_columns(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_instance
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_instance(cr, 1, {"invalid_col": "x"})

    def test_valid_column_calls_execute(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_instance
        cr = MagicMock()
        _update_instance(cr, 1, {"status": "built"})
        cr.execute.assert_called_once()

    def test_empty_vals_no_op(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_instance
        cr = MagicMock()
        _update_instance(cr, 1, {})
        cr.execute.assert_not_called()


class TestEvalCancelledException(unittest.TestCase):
    """Tests for EvalCancelled"""

    def test_is_exception(self):
        from odoo.addons.aurora.models.evaluation_executor import EvalCancelled
        self.assertTrue(issubclass(EvalCancelled, Exception))

    def test_message(self):
        from odoo.addons.aurora.models.evaluation_executor import EvalCancelled
        with self.assertRaises(EvalCancelled) as ctx:
            raise EvalCancelled("eval cancelled")
        self.assertEqual(str(ctx.exception), "eval cancelled")


class TestSubmitEvaluationAsync(unittest.TestCase):
    """Tests for submit_evaluation_async"""

    @patch("odoo.addons.aurora.models.evaluation_executor._executor")
    @patch("odoo.addons.aurora.models.evaluation_executor._semaphore")
    def test_acquires_semaphore_returns_true(self, mock_sem, mock_exec):
        from odoo.addons.aurora.models.evaluation_executor import submit_evaluation_async
        mock_sem.acquire.return_value = True
        result = submit_evaluation_async("db", 1, 10)
        self.assertTrue(result)
        mock_exec.submit.assert_called_once()

    @patch("odoo.addons.aurora.models.evaluation_executor._executor")
    @patch("odoo.addons.aurora.models.evaluation_executor._semaphore")
    def test_returns_false_when_full(self, mock_sem, mock_exec):
        from odoo.addons.aurora.models.evaluation_executor import submit_evaluation_async
        mock_sem.acquire.return_value = False
        result = submit_evaluation_async("db", 1, 10)
        self.assertFalse(result)
        mock_exec.submit.assert_not_called()


class TestSafePhaseHook(unittest.TestCase):
    """Tests for _safe_phase_hook"""

    def test_success_commits(self):
        from odoo.addons.aurora.models.evaluation_executor import _safe_phase_hook
        cr = MagicMock()
        fn = MagicMock()
        _safe_phase_hook(cr, 1, "test-phase", fn)
        fn.assert_called_once()
        cr.commit.assert_called()

    def test_failure_rollbacks_and_logs(self):
        from odoo.addons.aurora.models.evaluation_executor import _safe_phase_hook
        cr = MagicMock()
        fn = MagicMock(side_effect=RuntimeError("oops"))
        with patch("odoo.addons.aurora.models.evaluation_executor._append_log") as mock_log:
            _safe_phase_hook(cr, 1, "broken-phase", fn)
            cr.rollback.assert_called()
            mock_log.assert_called()


class TestEvalConstants(unittest.TestCase):
    """Tests for evaluation constants"""

    def test_inline_dockerfile_cap(self):
        from odoo.addons.aurora.models.evaluation_executor import _INLINE_DOCKERFILE_CAP
        self.assertEqual(_INLINE_DOCKERFILE_CAP, 16 * 1024)

    def test_inline_report_cap(self):
        from odoo.addons.aurora.models.evaluation_executor import _INLINE_REPORT_CAP
        self.assertEqual(_INLINE_REPORT_CAP, 128 * 1024)

    def test_inline_fix_patch_cap(self):
        from odoo.addons.aurora.models.evaluation_executor import _INLINE_FIX_PATCH_CAP
        self.assertEqual(_INLINE_FIX_PATCH_CAP, 256 * 1024)

    def test_log_tail_bytes(self):
        from odoo.addons.aurora.models.evaluation_executor import _LOG_TAIL_BYTES
        self.assertEqual(_LOG_TAIL_BYTES, 64 * 1024)

    def test_max_eval_threads(self):
        from odoo.addons.aurora.models.evaluation_executor import _MAX_EVAL_THREADS
        self.assertEqual(_MAX_EVAL_THREADS, 2)

    def test_max_concurrent_evals(self):
        from odoo.addons.aurora.models.evaluation_executor import _MAX_CONCURRENT_EVALS
        self.assertEqual(_MAX_CONCURRENT_EVALS, 2)


class TestPipelineAllowedColumnsExtended(unittest.TestCase):
    """Extended tests for pipeline _ALLOWED_COLUMNS content"""

    def test_contains_pr_count(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("pr_count", _ALLOWED_COLUMNS)

    def test_contains_dataset_url(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("dataset_url", _ALLOWED_COLUMNS)

    def test_contains_phase2_status(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase2_status", _ALLOWED_COLUMNS)

    def test_contains_phase3_pass_at_k(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase3_pass_at_k", _ALLOWED_COLUMNS)


class TestEvalAllowedColumnsExtended(unittest.TestCase):
    """Extended tests for eval _ALLOWED_COLUMNS content"""

    def test_contains_report_status(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("report_status", _ALLOWED_COLUMNS)

    def test_contains_resolved_instances(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("resolved_instances", _ALLOWED_COLUMNS)

    def test_contains_final_report_file(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("final_report_file", _ALLOWED_COLUMNS)

    def test_contains_s3_run_number(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("s3_run_number", _ALLOWED_COLUMNS)


class TestEvalInstanceColumnsExtended(unittest.TestCase):
    """Extended tests for _ALLOWED_INSTANCE_COLUMNS"""

    def test_contains_image_tag(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("image_tag", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_build_log_tail(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("build_log_tail", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_report_json_s3_uri(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("report_json_s3_uri", _ALLOWED_INSTANCE_COLUMNS)


class TestPipelineUpdateMultipleColumns(unittest.TestCase):

    def test_multiple_valid_columns_in_single_call(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 1, {"stage": "done", "progress_text": "finished", "pr_count": 10})
        cr.execute.assert_called_once()
        query = cr.execute.call_args[0][0]
        self.assertIn("stage", query)
        self.assertIn("progress_text", query)

    def test_mixed_valid_and_invalid_raises(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_pipeline(cr, 1, {"stage": "done", "hacked": "yes"})

    def test_params_order_matches_values(self):
        from odoo.addons.aurora.models.pipeline_executor import _update_pipeline
        cr = MagicMock()
        _update_pipeline(cr, 5, {"stage": "running"})
        params = cr.execute.call_args[0][1]
        self.assertEqual(params[-1], 5)
        self.assertEqual(params[0], "running")


class TestPipelineDbLogStreamOriginalStream(unittest.TestCase):

    def test_write_passes_to_original(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        orig = MagicMock()
        stream = _DbLogStream("db", 1, orig, flush_interval=999)
        stream.write("hi")
        orig.write.assert_called_once_with("hi")

    def test_flush_passes_to_original(self):
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        orig = MagicMock()
        stream = _DbLogStream("db", 1, orig, flush_interval=999)
        stream.flush()
        orig.flush.assert_called_once()

    def test_newline_handling(self):
        from datetime import datetime
        from odoo.addons.aurora.models.pipeline_executor import _DbLogStream
        stream = _DbLogStream("db", 1, None, flush_interval=999)
        stream._last_flush = datetime.now().timestamp()
        stream.write("line1\nline2\n")
        self.assertIn("line1\n", stream._buffer)
        self.assertIn("line2\n", stream._buffer)


class TestPipelineAppendLogNewline(unittest.TestCase):

    def test_log_line_ends_with_newline(self):
        from odoo.addons.aurora.models.pipeline_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "test")
        args = cr.execute.call_args[0][1]
        self.assertTrue(args[0].endswith("\n"))


class TestEvalUpdateMultipleColumns(unittest.TestCase):

    def test_multiple_valid_columns(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        _update_eval(cr, 1, {"stage": "done", "total_instances": 5})
        cr.execute.assert_called_once()

    def test_mixed_valid_invalid_raises(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_eval(cr, 1, {"stage": "done", "injected_col": "x"})

    def test_query_contains_update(self):
        from odoo.addons.aurora.models.evaluation_executor import _update_eval
        cr = MagicMock()
        _update_eval(cr, 1, {"progress_text": "working"})
        query = cr.execute.call_args[0][0]
        self.assertIn("UPDATE aurora_evaluation", query)


class TestEvalAppendLogNewline(unittest.TestCase):

    def test_log_line_ends_with_newline(self):
        from odoo.addons.aurora.models.evaluation_executor import _append_log
        cr = MagicMock()
        _append_log(cr, 1, "msg")
        args = cr.execute.call_args[0][1]
        self.assertTrue(args[0].endswith("\n"))

    def test_max_log_size_used(self):
        from odoo.addons.aurora.models.evaluation_executor import _append_log, _MAX_LOG_SIZE
        cr = MagicMock()
        _append_log(cr, 1, "x")
        args = cr.execute.call_args[0][1]
        self.assertEqual(args[1], _MAX_LOG_SIZE)


class TestEvalUpdateInstanceSerialization(unittest.TestCase):

    @patch("odoo.addons.aurora.models.evaluation_executor.time.sleep")
    def test_serialization_retry_on_instance(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.evaluation_executor import _update_instance
        cr = MagicMock()
        cr.execute.side_effect = [
            psycopg2.errors.SerializationFailure(),
            None,
        ]
        _update_instance(cr, 1, {"status": "built"})
        self.assertEqual(cr.execute.call_count, 2)

    @patch("odoo.addons.aurora.models.evaluation_executor.time.sleep")
    def test_serialization_exhausted_drops_gracefully(self, mock_sleep):
        import psycopg2.errors
        from odoo.addons.aurora.models.evaluation_executor import _update_instance
        cr = MagicMock()
        cr.execute.side_effect = psycopg2.errors.SerializationFailure()
        _update_instance(cr, 1, {"status": "built"})
        self.assertEqual(cr.execute.call_count, 3)


class TestPipelineAllowedColumnsPhase(unittest.TestCase):

    def test_contains_phase1_status(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase1_status", _ALLOWED_COLUMNS)

    def test_contains_phase1_file(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase1_file", _ALLOWED_COLUMNS)

    def test_contains_phase3_status(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase3_status", _ALLOWED_COLUMNS)

    def test_contains_phase2_dataset_count(self):
        from odoo.addons.aurora.models.pipeline_executor import _ALLOWED_COLUMNS
        self.assertIn("phase2_dataset_count", _ALLOWED_COLUMNS)


class TestEvalInstanceColumnsLogPaths(unittest.TestCase):

    def test_contains_run_log_s3_uri(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("run_log_s3_uri", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_fix_patch_content(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("fix_patch_content", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_test_patch_log_tail(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("test_patch_log_tail", _ALLOWED_INSTANCE_COLUMNS)

    def test_contains_dockerfile_local_path(self):
        from odoo.addons.aurora.models.evaluation_executor import _ALLOWED_INSTANCE_COLUMNS
        self.assertIn("dockerfile_local_path", _ALLOWED_INSTANCE_COLUMNS)


class TestPipelineCancellableSleepTimeout(unittest.TestCase):

    def test_cancellable_sleep_respects_short_timeout(self):
        from odoo.addons.aurora.models.pipeline_executor import (
            cancellable_sleep, _thread_cancel_events, _thread_cancel_lock,
        )
        event = threading.Event()
        tid = threading.current_thread().ident
        with _thread_cancel_lock:
            _thread_cancel_events[tid] = event
        try:
            start = time.time()
            cancellable_sleep(0.05)
            elapsed = time.time() - start
            self.assertGreaterEqual(elapsed, 0.04)
        finally:
            with _thread_cancel_lock:
                _thread_cancel_events.pop(tid, None)

    def test_count_jsonl_lines_empty_file(self):
        from odoo.addons.aurora.models.pipeline_executor import _count_jsonl_lines
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            self.assertEqual(_count_jsonl_lines(path), 0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
