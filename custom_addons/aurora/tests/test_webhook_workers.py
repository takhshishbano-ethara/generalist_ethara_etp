# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import os
import time
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock


class TestLegacyAuthEnabled(TestCase):

    def _call(self, val):
        from odoo.addons.aurora.controllers.webhook_controller import _legacy_auth_enabled
        env = MagicMock()
        icp = MagicMock()
        icp.get_param.return_value = val
        env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=icp)))
        return _legacy_auth_enabled(env)

    def test_true_string(self): self.assertTrue(self._call("True"))
    def test_true_lower(self): self.assertTrue(self._call("true"))
    def test_one_string(self): self.assertTrue(self._call("1"))
    def test_yes_string(self): self.assertTrue(self._call("yes"))
    def test_on_string(self): self.assertTrue(self._call("on"))
    def test_false_string(self): self.assertFalse(self._call("False"))
    def test_false_lower(self): self.assertFalse(self._call("false"))
    def test_zero_string(self): self.assertFalse(self._call("0"))
    def test_no_string(self): self.assertFalse(self._call("no"))
    def test_off_string(self): self.assertFalse(self._call("off"))
    def test_empty_string(self): self.assertFalse(self._call(""))
    def test_whitespace_true(self): self.assertTrue(self._call("  true  "))
    def test_random_text(self): self.assertFalse(self._call("maybe"))


class TestFilterPayload(TestCase):

    def test_filters_to_allowed(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        allowed = frozenset({"stage", "pr_count"})
        result = _filter_payload({"stage": "done", "pr_count": 5, "evil": "x"}, allowed)
        self.assertEqual(result, {"stage": "done", "pr_count": 5})

    def test_empty_payload(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload({}, frozenset({"stage"}))
        self.assertEqual(result, {})

    def test_non_dict_returns_empty(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload("not a dict", frozenset({"stage"}))
        self.assertEqual(result, {})

    def test_none_returns_empty(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload(None, frozenset({"stage"}))
        self.assertEqual(result, {})

    def test_all_disallowed(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload({"a": 1, "b": 2}, frozenset({"c"}))
        self.assertEqual(result, {})

    def test_all_allowed(self):
        from odoo.addons.aurora.controllers.webhook_controller import _filter_payload
        result = _filter_payload({"a": 1, "b": 2}, frozenset({"a", "b"}))
        self.assertEqual(result, {"a": 1, "b": 2})


class TestAllowedPipelineFields(TestCase):

    def test_stage_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertIn("stage", _ALLOWED_PIPELINE_FIELDS)

    def test_step_statuses_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        for i in range(1, 7):
            self.assertIn(f"step{i}_status", _ALLOWED_PIPELINE_FIELDS)

    def test_phase_statuses_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        for i in range(1, 4):
            self.assertIn(f"phase{i}_status", _ALLOWED_PIPELINE_FIELDS)

    def test_counts_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        for f in ["pr_count", "filtered_pr_count", "tag_count", "group_count", "issue_count", "dataset_count"]:
            self.assertIn(f, _ALLOWED_PIPELINE_FIELDS)

    def test_id_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertNotIn("id", _ALLOWED_PIPELINE_FIELDS)

    def test_job_name_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertNotIn("job_name", _ALLOWED_PIPELINE_FIELDS)

    def test_log_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_PIPELINE_FIELDS
        self.assertNotIn("log", _ALLOWED_PIPELINE_FIELDS)


class TestAllowedEvaluationFields(TestCase):

    def test_stage_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("stage", _ALLOWED_EVALUATION_FIELDS)

    def test_build_status_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("build_status", _ALLOWED_EVALUATION_FIELDS)

    def test_run_status_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("run_status", _ALLOWED_EVALUATION_FIELDS)

    def test_report_status_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertIn("report_status", _ALLOWED_EVALUATION_FIELDS)

    def test_id_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertNotIn("id", _ALLOWED_EVALUATION_FIELDS)

    def test_log_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_EVALUATION_FIELDS
        self.assertNotIn("log", _ALLOWED_EVALUATION_FIELDS)


class TestAllowedStagingFields(TestCase):

    def test_stage_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertIn("stage", _ALLOWED_STAGING_FIELDS)

    def test_test_result_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertIn("test_result", _ALLOWED_STAGING_FIELDS)

    def test_staging_path_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertIn("staging_path", _ALLOWED_STAGING_FIELDS)

    def test_exactly_3_fields(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertEqual(len(_ALLOWED_STAGING_FIELDS), 3)

    def test_id_not_in(self):
        from odoo.addons.aurora.controllers.webhook_controller import _ALLOWED_STAGING_FIELDS
        self.assertNotIn("id", _ALLOWED_STAGING_FIELDS)


class TestAppendLog(TestCase):

    def test_appends_message(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        record.log = "existing"
        _append_log(record, "new line")
        record.sudo.return_value.write.assert_called_once()
        written = record.sudo.return_value.write.call_args[0][0]["log"]
        self.assertIn("new line", written)

    def test_empty_message_no_op(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        _append_log(record, "")
        record.sudo.return_value.write.assert_not_called()

    def test_none_log_handled(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        record.log = None
        _append_log(record, "first")
        record.sudo.return_value.write.assert_called_once()

    def test_caps_at_5000_lines(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        record.log = "\n".join([f"line{i}" for i in range(5100)])
        _append_log(record, "overflow")
        written = record.sudo.return_value.write.call_args[0][0]["log"]
        self.assertLessEqual(len(written.splitlines()), 5000)

    def test_preserves_existing_content(self):
        from odoo.addons.aurora.controllers.webhook_controller import _append_log
        record = MagicMock()
        record.log = "first line"
        _append_log(record, "second line")
        written = record.sudo.return_value.write.call_args[0][0]["log"]
        self.assertIn("first line", written)


class TestSendBusNotification(TestCase):

    def test_sends_to_partner(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 1
        record.user_id.partner_id = MagicMock()
        record.stage = "done"
        _send_bus_notification(env, "aurora.pipeline", record, {"stage": "done"})
        env["bus.bus"].sudo.return_value._sendone.assert_called_once()

    def test_no_user_no_send(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 1
        record.user_id = None
        _send_bus_notification(env, "aurora.pipeline", record, {})
        env["bus.bus"].sudo.return_value._sendone.assert_not_called()

    def test_exception_swallowed(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        env["bus.bus"].sudo.return_value._sendone.side_effect = Exception("bus down")
        record = MagicMock()
        record.id = 1
        record.user_id.partner_id = MagicMock()
        _send_bus_notification(env, "aurora.pipeline", record, {})

    def test_payload_includes_model_and_id(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 42
        record.user_id.partner_id = MagicMock()
        record.stage = "running"
        record.progress_text = "Step 3"
        _send_bus_notification(env, "aurora.pipeline", record, {"stage": "running"})
        payload = env["bus.bus"].sudo.return_value._sendone.call_args[0][2]
        self.assertEqual(payload["model"], "aurora.pipeline")
        self.assertEqual(payload["id"], 42)

    def test_stage_from_values_preferred(self):
        from odoo.addons.aurora.controllers.webhook_controller import _send_bus_notification
        env = MagicMock()
        record = MagicMock()
        record.id = 1
        record.user_id.partner_id = MagicMock()
        record.stage = "old"
        _send_bus_notification(env, "m", record, {"stage": "new"})
        payload = env["bus.bus"].sudo.return_value._sendone.call_args[0][2]
        self.assertEqual(payload["stage"], "new")


class TestWebhookMaxSkew(TestCase):

    def test_max_skew_is_300(self):
        from odoo.addons.aurora.controllers.webhook_controller import _WEBHOOK_MAX_SKEW_SECONDS
        self.assertEqual(_WEBHOOK_MAX_SKEW_SECONDS, 300)


class TestRunPipelineIsTransientDbError(TestCase):

    def test_serialization_failure(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "40001"
        self.assertTrue(_is_transient_db_error(exc))

    def test_deadlock(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "40P01"
        self.assertTrue(_is_transient_db_error(exc))

    def test_connection_failure(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "08006"
        self.assertTrue(_is_transient_db_error(exc))

    def test_client_unable_connect(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "08001"
        self.assertTrue(_is_transient_db_error(exc))

    def test_non_transient_code(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = "23505"
        type(exc).__name__ = "IntegrityError"
        self.assertFalse(_is_transient_db_error(exc))

    def test_operational_error_no_pgcode(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = None
        type(exc).__name__ = "OperationalError"
        self.assertTrue(_is_transient_db_error(exc))

    def test_other_error_no_pgcode(self):
        from odoo.addons.aurora.worker.run_pipeline import _is_transient_db_error
        exc = MagicMock()
        exc.pgcode = None
        type(exc).__name__ = "ValueError"
        self.assertFalse(_is_transient_db_error(exc))


class TestRunPipelinePipelineCancelled(TestCase):

    def test_is_exception(self):
        from odoo.addons.aurora.worker.run_pipeline import PipelineCancelled
        self.assertTrue(issubclass(PipelineCancelled, Exception))

    def test_message(self):
        from odoo.addons.aurora.worker.run_pipeline import PipelineCancelled
        exc = PipelineCancelled("test msg")
        self.assertEqual(str(exc), "test msg")


class TestRunPipelineCheckCancelled(TestCase):

    def test_not_cancelled_no_raise(self):
        import odoo.addons.aurora.worker.run_pipeline as mod
        original = mod._cancelled
        mod._cancelled = False
        try:
            mod._check_cancelled()
        finally:
            mod._cancelled = original

    def test_cancelled_raises(self):
        import odoo.addons.aurora.worker.run_pipeline as mod
        original = mod._cancelled
        mod._cancelled = True
        try:
            with self.assertRaises(mod.PipelineCancelled):
                mod._check_cancelled()
        finally:
            mod._cancelled = original


class TestRunPipelineBuildS3Config(TestCase):

    def test_maps_keys(self):
        from odoo.addons.aurora.worker.run_pipeline import _build_s3_config
        cfg = {
            "s3_bucket": "bucket",
            "s3_access_key": "ak",
            "s3_secret_key": "sk",
            "s3_region": "us-east-1",
        }
        result = _build_s3_config(cfg)
        self.assertEqual(result["bucket"], "bucket")
        self.assertEqual(result["access_key"], "ak")
        self.assertEqual(result["secret_key"], "sk")
        self.assertEqual(result["region"], "us-east-1")

    def test_empty_values(self):
        from odoo.addons.aurora.worker.run_pipeline import _build_s3_config
        cfg = {"s3_bucket": "", "s3_access_key": "", "s3_secret_key": "", "s3_region": ""}
        result = _build_s3_config(cfg)
        self.assertEqual(result["bucket"], "")


class TestRunPipelineTransientConstants(TestCase):

    def test_transient_codes_set(self):
        from odoo.addons.aurora.worker.run_pipeline import _TRANSIENT_PG_CODES
        self.assertIn("40001", _TRANSIENT_PG_CODES)
        self.assertIn("40P01", _TRANSIENT_PG_CODES)
        self.assertIn("08006", _TRANSIENT_PG_CODES)
        self.assertIn("08001", _TRANSIENT_PG_CODES)

    def test_max_retries_3(self):
        from odoo.addons.aurora.worker.run_pipeline import _DB_WRITE_MAX_RETRIES
        self.assertEqual(_DB_WRITE_MAX_RETRIES, 3)

    def test_retry_base_delay(self):
        from odoo.addons.aurora.worker.run_pipeline import _DB_WRITE_RETRY_BASE_DELAY
        self.assertEqual(_DB_WRITE_RETRY_BASE_DELAY, 0.5)


class TestRunEvaluationEvalCancelled(TestCase):

    def test_is_exception(self):
        from odoo.addons.aurora.worker.run_evaluation import EvalCancelled
        self.assertTrue(issubclass(EvalCancelled, Exception))

    def test_message(self):
        from odoo.addons.aurora.worker.run_evaluation import EvalCancelled
        exc = EvalCancelled("stopped")
        self.assertEqual(str(exc), "stopped")


class TestRunEvaluationAllowedEvalColumns(TestCase):

    def test_stage_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("stage", _ALLOWED_EVAL_COLUMNS)

    def test_build_status_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("build_status", _ALLOWED_EVAL_COLUMNS)

    def test_run_status_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("run_status", _ALLOWED_EVAL_COLUMNS)

    def test_report_status_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("report_status", _ALLOWED_EVAL_COLUMNS)

    def test_last_heartbeat_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertIn("last_heartbeat", _ALLOWED_EVAL_COLUMNS)

    def test_id_not_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertNotIn("id", _ALLOWED_EVAL_COLUMNS)

    def test_evaluation_id_not_allowed(self):
        from odoo.addons.aurora.worker.run_evaluation import _ALLOWED_EVAL_COLUMNS
        self.assertNotIn("evaluation_id", _ALLOWED_EVAL_COLUMNS)


class TestRunEvaluationUpdateEval(TestCase):

    def test_empty_vals_no_op(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        _update_eval(conn, 1, {})
        conn.cursor.assert_not_called()

    def test_invalid_column_raises(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        with self.assertRaises(ValueError):
            _update_eval(conn, 1, {"hacker_col": "x"})

    def test_valid_columns_execute(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _update_eval(conn, 1, {"stage": "done"})
        cursor.execute.assert_called_once()

    def test_commits(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _update_eval(conn, 1, {"stage": "failed"})
        conn.commit.assert_called_once()

    def test_multiple_columns(self):
        from odoo.addons.aurora.worker.run_evaluation import _update_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _update_eval(conn, 1, {"stage": "done", "total_instances": 10})
        sql = cursor.execute.call_args[0][0]
        self.assertIn("stage = %s", sql)
        self.assertIn("total_instances = %s", sql)


class TestRunEvaluationResolveEntryNumber(TestCase):

    def test_int_number(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertEqual(_resolve_entry_number({"number": 42}), 42)

    def test_string_number(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertEqual(_resolve_entry_number({"number": "123"}), 123)

    def test_hyphenated_number(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertEqual(_resolve_entry_number({"number": "5-extra"}), 5)

    def test_pr_numbers_list(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertEqual(_resolve_entry_number({"pr_numbers": [7, 8]}), 7)

    def test_no_number_returns_none(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({}))

    def test_empty_pr_numbers(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"pr_numbers": []}))

    def test_non_numeric_string(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertIsNone(_resolve_entry_number({"number": "abc"}))

    def test_pr_numbers_string_elements(self):
        from odoo.addons.aurora.worker.run_evaluation import _resolve_entry_number
        self.assertEqual(_resolve_entry_number({"pr_numbers": ["99"]}), 99)


class TestRunEvaluationGeneratePatchFile(TestCase):

    def test_generates_patches(self):
        import tempfile, json
        from odoo.addons.aurora.worker.run_evaluation import _generate_patch_file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"org": "o", "repo": "r", "number": 1, "fix_patch": "diff"}) + "\n")
            f.write(json.dumps({"org": "o", "repo": "r", "number": 2, "fix_patch": "patch2"}) + "\n")
            src = f.name
        out = src + ".patches.jsonl"
        try:
            _generate_patch_file(src, out)
            with open(out) as fp:
                lines = fp.readlines()
            self.assertEqual(len(lines), 2)
            entry = json.loads(lines[0])
            self.assertEqual(entry["number"], 1)
            self.assertEqual(entry["fix_patch"], "diff")
        finally:
            os.unlink(src)
            if os.path.exists(out):
                os.unlink(out)

    def test_skips_entries_without_number(self):
        import tempfile, json
        from odoo.addons.aurora.worker.run_evaluation import _generate_patch_file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"org": "o", "repo": "r"}) + "\n")
            src = f.name
        out = src + ".patches.jsonl"
        try:
            _generate_patch_file(src, out)
            with open(out) as fp:
                lines = fp.readlines()
            self.assertEqual(len(lines), 0)
        finally:
            os.unlink(src)
            if os.path.exists(out):
                os.unlink(out)

    def test_empty_file(self):
        import tempfile
        from odoo.addons.aurora.worker.run_evaluation import _generate_patch_file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            src = f.name
        out = src + ".patches.jsonl"
        try:
            _generate_patch_file(src, out)
            with open(out) as fp:
                self.assertEqual(fp.read(), "")
        finally:
            os.unlink(src)
            if os.path.exists(out):
                os.unlink(out)

    def test_creates_output_dir(self):
        import tempfile, json
        from odoo.addons.aurora.worker.run_evaluation import _generate_patch_file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"org": "o", "repo": "r", "number": 1, "fix_patch": ""}) + "\n")
            src = f.name
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "subdir", "patches.jsonl")
            _generate_patch_file(src, out)
            self.assertTrue(os.path.isfile(out))
        os.unlink(src)


class TestRunEvaluationHeartbeat(TestCase):

    def test_updates_last_heartbeat(self):
        from odoo.addons.aurora.worker.run_evaluation import _heartbeat
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _heartbeat(conn, 1)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_with_progress_text(self):
        from odoo.addons.aurora.worker.run_evaluation import _heartbeat
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _heartbeat(conn, 1, "Building images")
        sql = cursor.execute.call_args[0][0]
        self.assertIn("progress_text", sql)

    def test_without_progress_text(self):
        from odoo.addons.aurora.worker.run_evaluation import _heartbeat
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _heartbeat(conn, 1, None)
        sql = cursor.execute.call_args[0][0]
        self.assertNotIn("progress_text", sql)


class TestRunEvaluationFailEval(TestCase):

    def test_calls_append_log_and_update(self):
        from odoo.addons.aurora.worker.run_evaluation import _fail_eval
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _fail_eval(conn, 1, "error happened")
        self.assertTrue(cursor.execute.call_count >= 2)
        conn.commit.assert_called()


class TestRunEvaluationAppendLog(TestCase):

    def test_appends_timestamped_line(self):
        from odoo.addons.aurora.worker.run_evaluation import _append_log
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _append_log(conn, 1, "hello")
        sql = cursor.execute.call_args[0][0]
        self.assertIn("aurora_evaluation", sql)
        params = cursor.execute.call_args[0][1]
        self.assertTrue(any("hello" in str(p) for p in params))

    def test_commits(self):
        from odoo.addons.aurora.worker.run_evaluation import _append_log
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        _append_log(conn, 1, "msg")
        conn.commit.assert_called_once()


class TestRunEvaluationReadEvalConfig(TestCase):

    def test_missing_record_raises(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        with self.assertRaises(RuntimeError):
            _read_eval_config(conn, 999)

    def test_valid_record_returns_dict(self):
        from odoo.addons.aurora.worker.run_evaluation import _read_eval_config
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "/data.jsonl", "/patches.jsonl", "/repos", "/work",
            "/output", False, 4, 4, "linux/amd64", 0, "", 1,
        )
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        result = _read_eval_config(conn, 1)
        self.assertEqual(result["dataset_file"], "/data.jsonl")
        self.assertEqual(result["docker_platform"], "linux/amd64")
        self.assertEqual(result["pipeline_id"], 1)
