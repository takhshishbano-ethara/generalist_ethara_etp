# -*- coding: utf-8 -*-
import io
import threading
from datetime import datetime
from unittest.mock import patch, MagicMock, call

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEvaluationExecutor(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("aurora.output_dir", "/tmp/aurora_test")
        cls.pipeline = cls.env["aurora.pipeline"].create({
            "github_org": "testorg", "github_repo": "testrepo",
        })

    def _create_eval(self, **kwargs):
        vals = {"pipeline_id": self.pipeline.id}
        vals.update(kwargs)
        return self.env["aurora.evaluation"].create(vals)

    # ═══════════════════════════════════════════════════════════════════════════
    # Column whitelist
    # ═══════════════════════════════════════════════════════════════════════════

    def test_allowed_columns_is_frozenset(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIsInstance(_ALLOWED_COLUMNS, frozenset)

    def test_allowed_columns_immutable(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        with self.assertRaises(AttributeError):
            _ALLOWED_COLUMNS.add("hack")

    def test_allowed_columns_contains_stage(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("stage", _ALLOWED_COLUMNS)

    def test_allowed_columns_contains_statuses(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        for col in ["build_status", "run_status", "report_status"]:
            self.assertIn(col, _ALLOWED_COLUMNS)

    def test_allowed_columns_contains_log(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("log", _ALLOWED_COLUMNS)

    def test_allowed_columns_contains_heartbeat(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("last_heartbeat", _ALLOWED_COLUMNS)
        self.assertIn("progress_text", _ALLOWED_COLUMNS)

    def test_allowed_columns_contains_counters(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        for col in ["total_instances", "resolved_instances", "unresolved_instances", "error_instances"]:
            self.assertIn(col, _ALLOWED_COLUMNS)

    def test_allowed_columns_contains_report_fields(self):
        from ..models.evaluation_executor import _ALLOWED_COLUMNS
        self.assertIn("final_report_file", _ALLOWED_COLUMNS)
        self.assertIn("missing_registries", _ALLOWED_COLUMNS)

    # ═══════════════════════════════════════════════════════════════════════════
    # _update_eval
    # ═══════════════════════════════════════════════════════════════════════════

    def test_update_eval_allowed_column(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        _update_eval(self.env.cr, rec.id, {"progress_text": "test"})
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertEqual(rec.progress_text, "test")

    def test_update_eval_rejects_invalid(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        with self.assertRaises(ValueError):
            _update_eval(self.env.cr, rec.id, {"evil_column": "hacked"})

    def test_update_eval_rejects_sql_injection(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        with self.assertRaises(ValueError):
            _update_eval(self.env.cr, rec.id, {"stage; DROP TABLE--": "x"})

    def test_update_eval_multiple_columns(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        _update_eval(self.env.cr, rec.id, {
            "stage": "building_images",
            "build_status": "running",
            "progress_text": "Starting",
        })
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertEqual(rec.stage, "building_images")
        self.assertEqual(rec.build_status, "running")
        self.assertEqual(rec.progress_text, "Starting")

    def test_update_eval_empty_vals(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        _update_eval(self.env.cr, rec.id, {})

    def test_update_eval_counter_fields(self):
        from ..models.evaluation_executor import _update_eval
        rec = self._create_eval()
        _update_eval(self.env.cr, rec.id, {
            "total_instances": 100,
            "resolved_instances": 50,
            "unresolved_instances": 30,
            "error_instances": 20,
        })
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertEqual(rec.total_instances, 100)
        self.assertEqual(rec.resolved_instances, 50)
        self.assertEqual(rec.unresolved_instances, 30)
        self.assertEqual(rec.error_instances, 20)

    # ═══════════════════════════════════════════════════════════════════════════
    # _append_log
    # ═══════════════════════════════════════════════════════════════════════════

    def test_append_log(self):
        from ..models.evaluation_executor import _append_log
        rec = self._create_eval()
        _append_log(self.env.cr, rec.id, "Hello world")
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertIn("Hello world", rec.log)

    def test_append_log_to_empty(self):
        from ..models.evaluation_executor import _append_log
        rec = self._create_eval()
        self.assertFalse(rec.log)
        _append_log(self.env.cr, rec.id, "First entry")
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertIn("First entry", rec.log)

    def test_append_log_multiple(self):
        from ..models.evaluation_executor import _append_log
        rec = self._create_eval()
        _append_log(self.env.cr, rec.id, "Entry 1")
        _append_log(self.env.cr, rec.id, "Entry 2")
        _append_log(self.env.cr, rec.id, "Entry 3")
        self.env.cr.flush()
        rec.invalidate_recordset()
        self.assertIn("Entry 1", rec.log)
        self.assertIn("Entry 2", rec.log)
        self.assertIn("Entry 3", rec.log)

    def test_append_log_has_timestamp(self):
        from ..models.evaluation_executor import _append_log
        rec = self._create_eval()
        _append_log(self.env.cr, rec.id, "timestamped")
        self.env.cr.flush()
        rec.invalidate_recordset()
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertIn(today, rec.log)

    # ═══════════════════════════════════════════════════════════════════════════
    # _heartbeat
    # ═══════════════════════════════════════════════════════════════════════════

    def test_heartbeat_updates_timestamp(self):
        from ..models.evaluation_executor import _heartbeat
        rec = self._create_eval()
        self.assertFalse(rec.last_heartbeat)
        with patch.object(self.env.cr, 'commit'):
            _heartbeat(self.env.cr, rec.id, "Working")
        rec.invalidate_recordset()
        self.assertTrue(rec.last_heartbeat)
        self.assertEqual(rec.progress_text, "Working")

    def test_heartbeat_without_progress(self):
        from ..models.evaluation_executor import _heartbeat
        rec = self._create_eval()
        with patch.object(self.env.cr, 'commit'):
            _heartbeat(self.env.cr, rec.id)
        rec.invalidate_recordset()
        self.assertTrue(rec.last_heartbeat)

    # ═══════════════════════════════════════════════════════════════════════════
    # _fail_eval
    # ═══════════════════════════════════════════════════════════════════════════

    def test_fail_eval_sets_status_and_stage(self):
        from ..models.evaluation_executor import _fail_eval
        rec = self._create_eval()
        rec.write({"stage": "building_images", "build_status": "running"})
        self.env.cr.flush()
        _fail_eval(self.env.cr, rec.id, "build_status", Exception("test error"))
        rec.invalidate_recordset()
        self.assertEqual(rec.build_status, "failed")
        self.assertEqual(rec.stage, "failed")

    def test_fail_eval_appends_log(self):
        from ..models.evaluation_executor import _fail_eval
        rec = self._create_eval()
        self.env.cr.flush()
        _fail_eval(self.env.cr, rec.id, "run_status", Exception("disk full"))
        rec.invalidate_recordset()
        self.assertIn("FAILED", rec.log)
        self.assertIn("disk full", rec.log)

    def test_fail_eval_each_status(self):
        from ..models.evaluation_executor import _fail_eval
        for field in ["build_status", "run_status", "report_status"]:
            rec = self._create_eval()
            rec.write({field: "running"})
            self.env.cr.flush()
            _fail_eval(self.env.cr, rec.id, field, Exception(f"fail {field}"))
            rec.invalidate_recordset()
            self.assertEqual(getattr(rec, field), "failed")

    # ═══════════════════════════════════════════════════════════════════════════
    # Cancel registry
    # ═══════════════════════════════════════════════════════════════════════════

    def test_cancel_event_lifecycle(self):
        from ..models.evaluation_executor import (
            _register_cancel_event, _unregister_cancel_event, request_cancel
        )
        event = _register_cancel_event(99998)
        self.assertIsInstance(event, threading.Event)
        self.assertFalse(event.is_set())
        result = request_cancel(99998)
        self.assertTrue(result)
        self.assertTrue(event.is_set())
        _unregister_cancel_event(99998)
        result = request_cancel(99998)
        self.assertFalse(result)

    def test_cancel_nonexistent(self):
        from ..models.evaluation_executor import request_cancel
        self.assertFalse(request_cancel(-2))

    def test_cancel_multiple_evals(self):
        from ..models.evaluation_executor import (
            _register_cancel_event, _unregister_cancel_event, request_cancel
        )
        e1 = _register_cancel_event(77771)
        e2 = _register_cancel_event(77772)
        try:
            request_cancel(77771)
            self.assertTrue(e1.is_set())
            self.assertFalse(e2.is_set())
            request_cancel(77772)
            self.assertTrue(e2.is_set())
        finally:
            _unregister_cancel_event(77771)
            _unregister_cancel_event(77772)

    def test_unregister_nonexistent_safe(self):
        from ..models.evaluation_executor import _unregister_cancel_event
        _unregister_cancel_event(66666)

    # ═══════════════════════════════════════════════════════════════════════════
    # EvalCancelled
    # ═══════════════════════════════════════════════════════════════════════════

    def test_eval_cancelled_is_exception(self):
        from ..models.evaluation_executor import EvalCancelled
        exc = EvalCancelled("test cancel")
        self.assertIsInstance(exc, Exception)
        self.assertEqual(str(exc), "test cancel")

    def test_eval_cancelled_can_be_caught(self):
        from ..models.evaluation_executor import EvalCancelled
        with self.assertRaises(EvalCancelled):
            raise EvalCancelled("user cancelled")

    # ═══════════════════════════════════════════════════════════════════════════
    # submit_evaluation_async
    # ═══════════════════════════════════════════════════════════════════════════

    def test_submit_returns_true(self):
        from ..models import evaluation_executor
        with patch.object(evaluation_executor, "_executor") as mock_exec:
            result = evaluation_executor.submit_evaluation_async("testdb", 1, 1)
            self.assertTrue(result)
            mock_exec.submit.assert_called_once()

    def test_submit_returns_false_when_full(self):
        from ..models import evaluation_executor
        original = evaluation_executor._semaphore
        evaluation_executor._semaphore = MagicMock()
        evaluation_executor._semaphore.acquire.return_value = False
        try:
            result = evaluation_executor.submit_evaluation_async("testdb", 1, 1)
            self.assertFalse(result)
        finally:
            evaluation_executor._semaphore = original

    def test_submit_passes_correct_args(self):
        from ..models import evaluation_executor
        with patch.object(evaluation_executor, "_executor") as mock_exec:
            evaluation_executor.submit_evaluation_async("mydb", 42, 99)
            args = mock_exec.submit.call_args
            self.assertEqual(args[0][1], "mydb")
            self.assertEqual(args[0][2], 42)
            self.assertEqual(args[0][3], 99)

    # ═══════════════════════════════════════════════════════════════════════════
    # _notify_bus
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_notify_bus_sends(self, mock_open):
        from ..models.evaluation_executor import _notify_bus
        mock_cr = MagicMock()
        mock_open.return_value = mock_cr
        mock_env = MagicMock()
        with patch("odoo.api.Environment", return_value=mock_env):
            _notify_bus("testdb", 42)
        mock_env['bus.bus']._sendone.assert_called_once()
        mock_cr.commit.assert_called()
        mock_cr.close.assert_called()

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_notify_bus_handles_error(self, mock_open):
        from ..models.evaluation_executor import _notify_bus
        mock_open.side_effect = Exception("DB down")
        _notify_bus("testdb", 42)

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_notify_bus_closes_cursor_on_error(self, mock_open):
        from ..models.evaluation_executor import _notify_bus
        mock_cr = MagicMock()
        mock_open.return_value = mock_cr
        with patch("odoo.api.Environment", side_effect=Exception("fail")):
            _notify_bus("testdb", 42)
        mock_cr.close.assert_called()

    # ═══════════════════════════════════════════════════════════════════════════
    # _post_chatter
    # ═══════════════════════════════════════════════════════════════════════════

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_post_chatter_sends(self, mock_open):
        from ..models.evaluation_executor import _post_chatter
        mock_cr = MagicMock()
        mock_open.return_value = mock_cr
        mock_env = MagicMock()
        mock_rec = MagicMock()
        mock_env.__getitem__.return_value.browse.return_value = mock_rec
        with patch("odoo.api.Environment", return_value=mock_env):
            _post_chatter("testdb", 1, 42, "Done")
        mock_rec.message_post.assert_called_once()
        mock_cr.commit.assert_called()
        mock_cr.close.assert_called()

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_post_chatter_handles_error(self, mock_open):
        from ..models.evaluation_executor import _post_chatter
        mock_open.side_effect = Exception("DB down")
        _post_chatter("testdb", 1, 42, "test")

    @patch("odoo.addons.aurora.models.evaluation_executor._open_cursor")
    def test_post_chatter_superuser_fallback(self, mock_open):
        from ..models.evaluation_executor import _post_chatter
        mock_cr = MagicMock()
        mock_open.return_value = mock_cr
        mock_env = MagicMock()
        with patch("odoo.api.Environment", return_value=mock_env) as mock_cls:
            _post_chatter("testdb", None, 42, "test")
            self.assertEqual(mock_cls.call_args[0][1], 1)

    # ═══════════════════════════════════════════════════════════════════════════
    # _safe_worker
    # ═══════════════════════════════════════════════════════════════════════════

    def test_safe_worker_releases_on_success(self):
        from ..models.evaluation_executor import _safe_worker, _semaphore
        call_count = [0]

        @_safe_worker
        def dummy(db, uid, rec_id):
            call_count[0] += 1

        initial = _semaphore._value
        _semaphore.acquire(blocking=False)
        dummy("testdb", 1, 1)
        self.assertEqual(call_count[0], 1)
        self.assertEqual(_semaphore._value, initial)

    def test_safe_worker_releases_on_failure(self):
        from ..models.evaluation_executor import _safe_worker, _semaphore

        @_safe_worker
        def failing(db, uid, rec_id):
            raise RuntimeError("boom")

        initial = _semaphore._value
        _semaphore.acquire(blocking=False)
        with patch("odoo.addons.aurora.models.evaluation_executor._open_cursor") as mock_cursor:
            mock_cr = MagicMock()
            mock_cursor.return_value = mock_cr
            failing("testdb", 1, 1)
        self.assertEqual(_semaphore._value, initial)

    # ═══════════════════════════════════════════════════════════════════════════
    # _read_eval_config
    # ═══════════════════════════════════════════════════════════════════════════

    def test_read_eval_config_keys(self):
        from ..models.evaluation_executor import _read_eval_config
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            output_dir="/tmp/out",
            workdir="/tmp/work",
            repo_dir="/tmp/repos",
        )
        with patch("odoo.addons.aurora.models.evaluation_executor._open_cursor") as mock:
            mock.return_value = self.env.cr
            with patch.object(self.env.cr, 'close'):
                cfg = _read_eval_config(self.env.cr.dbname, rec.id)
        expected_keys = {
            "dataset_file", "patch_file", "repo_dir", "workdir", "output_dir",
            "force_build", "max_workers_build", "max_workers_run",
            "docker_platform", "instance_limit", "specific_prs",
        }
        self.assertEqual(set(cfg.keys()), expected_keys)

    def test_read_eval_config_values(self):
        from ..models.evaluation_executor import _read_eval_config
        rec = self._create_eval(
            dataset_file="/tmp/ds.jsonl",
            output_dir="/tmp/out",
            max_workers_build=8,
        )
        with patch("odoo.addons.aurora.models.evaluation_executor._open_cursor") as mock:
            mock.return_value = self.env.cr
            with patch.object(self.env.cr, 'close'):
                cfg = _read_eval_config(self.env.cr.dbname, rec.id)
        self.assertEqual(cfg["dataset_file"], "/tmp/ds.jsonl")
        self.assertEqual(cfg["output_dir"], "/tmp/out")
        self.assertEqual(cfg["max_workers_build"], 8)

    def test_read_eval_config_nonexistent_raises(self):
        from ..models.evaluation_executor import _read_eval_config
        with patch("odoo.addons.aurora.models.evaluation_executor._open_cursor") as mock:
            mock.return_value = self.env.cr
            with patch.object(self.env.cr, 'close'):
                with self.assertRaises(RuntimeError):
                    _read_eval_config(self.env.cr.dbname, 999999)

    # ═══════════════════════════════════════════════════════════════════════════
    # Constants
    # ═══════════════════════════════════════════════════════════════════════════

    def test_constants_positive(self):
        from ..models.evaluation_executor import _MAX_EVAL_THREADS, _MAX_CONCURRENT_EVALS
        self.assertGreater(_MAX_EVAL_THREADS, 0)
        self.assertGreater(_MAX_CONCURRENT_EVALS, 0)

    def test_constants_are_integers(self):
        from ..models.evaluation_executor import _MAX_EVAL_THREADS, _MAX_CONCURRENT_EVALS
        self.assertIsInstance(_MAX_EVAL_THREADS, int)
        self.assertIsInstance(_MAX_CONCURRENT_EVALS, int)

    def test_max_log_size_positive(self):
        from ..models.evaluation_executor import _MAX_LOG_SIZE
        self.assertGreater(_MAX_LOG_SIZE, 0)

    def test_serialization_retries_positive(self):
        from ..models.evaluation_executor import _SERIALIZATION_RETRIES
        self.assertGreater(_SERIALIZATION_RETRIES, 0)
