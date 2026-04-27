import json
import re
from unittest.mock import MagicMock, call

import pytest

from aurora.models.pipeline_executor import (
    _ALLOWED_COLUMNS,
    _MAX_LOG_SIZE,
    _append_log,
    _append_step_log,
    _count_jsonl_lines,
    _fail_pipeline,
    _heartbeat,
    _update_pipeline,
    _validate_step_output,
)
from aurora.tools.util import AuroraPipelineError


# ---------------------------------------------------------------------------
# _ALLOWED_COLUMNS
# ---------------------------------------------------------------------------

_EXPECTED_COLUMNS = frozenset({
    "step1_status", "step1_file", "step2_status", "step2_file",
    "step3_status", "step3_file", "step4_status", "step4_file",
    "step5_status", "step5_file", "step6_status", "step6_file",
    "step1_log", "step2_log", "step3_log", "step4_log", "step5_log", "step6_log",
    "stage", "pr_count", "filtered_pr_count", "tag_count",
    "group_count", "issue_count", "dataset_count",
    "dataset_url", "dataset_filename", "progress_text",
    "last_heartbeat",
    "phase1_status", "phase1_file",
    "phase2_status", "phase2_file", "phase2_image_count",
    "phase2_instance_count", "phase2_resolved_count",
    "phase2_log", "phase2_has_registry",
    "phase3_status", "phase3_file", "phase3_inference_count",
    "phase3_pass_at_k", "phase3_log",
})


class TestAllowedColumns:
    def test_is_frozenset(self):
        assert isinstance(_ALLOWED_COLUMNS, frozenset)

    def test_exact_membership(self):
        assert _ALLOWED_COLUMNS == _EXPECTED_COLUMNS

    def test_column_count(self):
        assert len(_ALLOWED_COLUMNS) == 43

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
    def test_step_columns_present(self, n):
        assert f"step{n}_status" in _ALLOWED_COLUMNS
        assert f"step{n}_file" in _ALLOWED_COLUMNS


# ---------------------------------------------------------------------------
# _update_pipeline
# ---------------------------------------------------------------------------

class TestUpdatePipeline:

    def test_empty_vals_noop(self, mock_cursor):
        _update_pipeline(mock_cursor, 1, {})
        mock_cursor.execute.assert_not_called()

    @pytest.mark.parametrize("col", [
        "stage", "pr_count", "filtered_pr_count", "tag_count",
        "step1_status", "step1_file", "step2_status", "step3_log",
        "step4_status", "step5_file", "step6_log",
        "dataset_url", "dataset_filename", "progress_text",
        "last_heartbeat", "phase1_status", "phase2_status",
        "phase2_log", "phase3_status", "phase3_pass_at_k",
    ])
    def test_single_allowed_column(self, mock_cursor, col):
        _update_pipeline(mock_cursor, 42, {col: "val"})
        sql = mock_cursor.execute.call_args[0][0]
        assert f"{col} = %s" in sql
        assert "WHERE id = %s" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert params == ["val", 42]

    @pytest.mark.parametrize("cols,vals", [
        (["stage", "pr_count"], ["running", 10]),
        (["step1_status", "step1_file"], ["done", "/tmp/a"]),
        (["tag_count", "group_count", "issue_count"], [5, 3, 2]),
        (["dataset_url", "dataset_filename"], ["http://x", "f.jsonl"]),
        (["phase2_status", "phase2_file", "phase2_image_count"], ["ok", "/p", 9]),
        (["step2_log", "step3_log"], ["log2", "log3"]),
        (["phase3_status", "phase3_file", "phase3_inference_count", "phase3_pass_at_k"], ["ok", "/x", 5, 0.8]),
        (["phase2_has_registry", "phase2_resolved_count", "phase2_instance_count"], [True, 3, 5]),
    ])
    def test_multi_column_update(self, mock_cursor, cols, vals):
        d = dict(zip(cols, vals))
        _update_pipeline(mock_cursor, 1, d)
        sql = mock_cursor.execute.call_args[0][0]
        sorted_keys = sorted(cols)
        for k in sorted_keys:
            assert f"{k} = %s" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert params[-1] == 1
        assert len(params) == len(cols) + 1

    @pytest.mark.parametrize("bad_col", [
        "id", "name", "user_id", "github_org", "github_repo",
        "create_uid", "write_uid", "create_date", "write_date",
        "password", "token", "secret", "api_key", "lang",
        "output_dir",
    ])
    def test_disallowed_column_raises(self, mock_cursor, bad_col):
        with pytest.raises(ValueError, match="disallowed columns"):
            _update_pipeline(mock_cursor, 1, {bad_col: "x"})
        mock_cursor.execute.assert_not_called()

    @pytest.mark.parametrize("bad_col,good_col", [
        ("id", "stage"),
        ("name", "pr_count"),
        ("user_id", "step1_status"),
        ("github_org", "step2_file"),
        ("password", "phase2_log"),
    ])
    def test_mixed_allowed_disallowed_raises(self, mock_cursor, bad_col, good_col):
        with pytest.raises(ValueError, match="disallowed columns"):
            _update_pipeline(mock_cursor, 1, {bad_col: "x", good_col: "y"})
        mock_cursor.execute.assert_not_called()

    @pytest.mark.parametrize("cols", [
        ["dataset_count", "stage"],
        ["phase2_status", "step1_file", "tag_count"],
        ["last_heartbeat", "progress_text"],
    ])
    def test_sql_set_clause_alphabetical(self, mock_cursor, cols):
        d = {c: "v" for c in cols}
        _update_pipeline(mock_cursor, 1, d)
        sql = mock_cursor.execute.call_args[0][0]
        sorted_cols = sorted(cols)
        set_part = sql.split("SET ")[1].split(" WHERE")[0]
        found_cols = [s.strip().split(" = ")[0] for s in set_part.split(",")]
        assert found_cols == sorted_cols

    def test_params_alphabetical_order(self, mock_cursor):
        d = {"stage": "done", "pr_count": 5, "dataset_count": 3}
        _update_pipeline(mock_cursor, 99, d)
        params = mock_cursor.execute.call_args[0][1]
        assert params == [3, 5, "done", 99]

    @pytest.mark.parametrize("val,label", [
        (None, "none"),
        ("", "empty_str"),
        (0, "zero"),
        (-1, "negative"),
        ("x" * 10_000, "long_str"),
        ("café ñ 日本", "unicode"),
        (True, "bool_true"),
        (False, "bool_false"),
    ])
    def test_special_values(self, mock_cursor, val, label):
        _update_pipeline(mock_cursor, 1, {"stage": val})
        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is val or params[0] == val

    @pytest.mark.parametrize("rec_id", [0, -1, 999_999_999, 1])
    def test_rec_id_edge_cases(self, mock_cursor, rec_id):
        _update_pipeline(mock_cursor, rec_id, {"stage": "ok"})
        params = mock_cursor.execute.call_args[0][1]
        assert params[-1] == rec_id


# ---------------------------------------------------------------------------
# _append_log
# ---------------------------------------------------------------------------

class TestAppendLog:

    @pytest.mark.parametrize("msg", [
        "Pipeline started",
        "Step 1 complete",
        "Fetched 42 PRs",
        "No issues found",
        "Building dataset...",
        "Upload to S3 finished",
        "Phase 2 starting",
        "Tag discovery complete",
        "Running step 6",
        "All steps passed",
    ])
    def test_normal_messages(self, mock_cursor, msg):
        _append_log(mock_cursor, 1, msg)
        sql = mock_cursor.execute.call_args[0][0]
        assert "RIGHT(COALESCE(log, '') || %s, %s)" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert msg in params[0]
        assert params[0].endswith("\n")
        assert params[1] == _MAX_LOG_SIZE
        assert params[2] == 1

    @pytest.mark.parametrize("msg", [
        "",
        "line1\nline2\nline3",
        "'; DROP TABLE aurora_pipeline; --",
        "café ☕ 日本語 العربية",
        "🚀🎉💥",
        "tab\there",
        "null\x00byte",
        "\r\nwindows\r\n",
    ])
    def test_special_char_messages(self, mock_cursor, msg):
        _append_log(mock_cursor, 5, msg)
        params = mock_cursor.execute.call_args[0][1]
        assert msg in params[0]
        assert params[2] == 5

    def test_very_long_message(self, mock_cursor):
        msg = "A" * 600_000
        _append_log(mock_cursor, 1, msg)
        params = mock_cursor.execute.call_args[0][1]
        assert msg in params[0]
        assert params[1] == _MAX_LOG_SIZE

    def test_timestamp_format(self, mock_cursor):
        _append_log(mock_cursor, 1, "test")
        line = mock_cursor.execute.call_args[0][1][0]
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] test\n$", line)

    def test_max_log_size_value(self):
        assert _MAX_LOG_SIZE == 500_000


# ---------------------------------------------------------------------------
# _append_step_log
# ---------------------------------------------------------------------------

class TestAppendStepLog:

    @pytest.mark.parametrize("step_num", [1, 2, 3, 4, 5, 6])
    def test_valid_step_numbers(self, mock_cursor, step_num):
        _append_step_log(mock_cursor, 10, step_num, "msg")
        sql = mock_cursor.execute.call_args[0][0]
        col = f"step{step_num}_log"
        assert col in sql
        assert "RIGHT(COALESCE(" in sql

    @pytest.mark.parametrize("step_num", [0, 7, -1, 100, 999])
    def test_invalid_step_numbers_no_sql(self, mock_cursor, step_num):
        _append_step_log(mock_cursor, 1, step_num, "msg")
        mock_cursor.execute.assert_not_called()

    @pytest.mark.parametrize("msg", [
        "processing",
        "fetch complete",
        "error occurred",
        "retrying...",
        "'; DROP TABLE x; --",
        "unicode: 日本語",
        "emoji: 🔥",
        "multi\nline\ntext",
    ])
    def test_message_parametrize(self, mock_cursor, msg):
        _append_step_log(mock_cursor, 1, 1, msg)
        params = mock_cursor.execute.call_args[0][1]
        assert msg in params[0]

    def test_timestamp_format_hms(self, mock_cursor):
        _append_step_log(mock_cursor, 1, 3, "check")
        line = mock_cursor.execute.call_args[0][1][0]
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] check\n$", line)

    def test_max_log_size_passed(self, mock_cursor):
        _append_step_log(mock_cursor, 1, 2, "x")
        params = mock_cursor.execute.call_args[0][1]
        assert params[1] == _MAX_LOG_SIZE

    def test_rec_id_passed(self, mock_cursor):
        _append_step_log(mock_cursor, 77, 4, "y")
        params = mock_cursor.execute.call_args[0][1]
        assert params[2] == 77

    @pytest.mark.parametrize("step_num", [1, 2, 3, 4, 5, 6])
    def test_column_name_generation(self, mock_cursor, step_num):
        _append_step_log(mock_cursor, 1, step_num, "t")
        sql = mock_cursor.execute.call_args[0][0]
        expected_col = f"step{step_num}_log"
        assert f"SET {expected_col} = RIGHT" in sql


# ---------------------------------------------------------------------------
# _heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:

    @pytest.mark.parametrize("text", [
        "Processing step 1",
        "Fetching PRs (page 3/10)",
        "Building dataset",
        "Uploading to S3",
        "Waiting for rate limit",
        "Running inference",
        "Generating report",
        "Cloning repository",
        "Filtering tags",
        "50% complete",
    ])
    def test_with_progress_text(self, mock_cursor, text):
        _heartbeat(mock_cursor, 1, progress_text=text)
        sql = mock_cursor.execute.call_args[0][0]
        assert "last_heartbeat = %s" in sql
        assert "progress_text = %s" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert text in params

    def test_without_progress_text(self, mock_cursor):
        _heartbeat(mock_cursor, 1)
        sql = mock_cursor.execute.call_args[0][0]
        assert "last_heartbeat = %s" in sql
        assert "progress_text" not in sql

    def test_none_progress_text(self, mock_cursor):
        _heartbeat(mock_cursor, 1, progress_text=None)
        sql = mock_cursor.execute.call_args[0][0]
        assert "progress_text" not in sql

    def test_heartbeat_timestamp_format(self, mock_cursor):
        _heartbeat(mock_cursor, 1)
        params = mock_cursor.execute.call_args[0][1]
        ts = params[0]
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts)

    def test_rec_id_forwarded(self, mock_cursor):
        _heartbeat(mock_cursor, 55)
        params = mock_cursor.execute.call_args[0][1]
        assert params[-1] == 55

    def test_empty_string_progress_text(self, mock_cursor):
        _heartbeat(mock_cursor, 1, progress_text="")
        sql = mock_cursor.execute.call_args[0][0]
        assert "progress_text = %s" in sql


# ---------------------------------------------------------------------------
# _fail_pipeline
# ---------------------------------------------------------------------------

class TestFailPipeline:

    @pytest.mark.parametrize("step_field", [
        "step1_status", "step2_status", "step4_status",
        "step6_status", "phase2_status", "phase3_status",
    ])
    @pytest.mark.parametrize("exc_type", [
        Exception, AuroraPipelineError,
    ])
    def test_step_field_and_exc_type(self, mock_cursor, step_field, exc_type):
        exc = exc_type("boom")
        _fail_pipeline(mock_cursor, 1, step_field, exc)
        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2
        update_sql = calls[0][0][0]
        assert f"{step_field} = %s" in update_sql
        assert "stage = %s" in update_sql
        update_params = calls[0][0][1]
        assert "failed" in update_params
        log_sql = calls[1][0][0]
        assert "log" in log_sql
        log_params = calls[1][0][1]
        assert "FAILED" in log_params[0]
        assert step_field in log_params[0]

    def test_fail_sets_stage_failed(self, mock_cursor):
        _fail_pipeline(mock_cursor, 1, "step1_status", Exception("err"))
        params = mock_cursor.execute.call_args_list[0][0][1]
        sorted_vals = dict(zip(
            sorted(["step1_status", "stage"]),
            params[:-1]
        ))
        assert sorted_vals["stage"] == "failed"
        assert sorted_vals["step1_status"] == "failed"

    def test_fail_rec_id(self, mock_cursor):
        _fail_pipeline(mock_cursor, 88, "step3_status", RuntimeError("x"))
        update_params = mock_cursor.execute.call_args_list[0][0][1]
        assert update_params[-1] == 88
        log_params = mock_cursor.execute.call_args_list[1][0][1]
        assert log_params[2] == 88

    def test_fail_exc_message_in_log(self, mock_cursor):
        _fail_pipeline(mock_cursor, 1, "step2_status", ValueError("custom error text"))
        log_line = mock_cursor.execute.call_args_list[1][0][1][0]
        assert "custom error text" in log_line

    def test_fail_with_runtime_error(self, mock_cursor):
        _fail_pipeline(mock_cursor, 1, "step5_status", RuntimeError("oops"))
        log_line = mock_cursor.execute.call_args_list[1][0][1][0]
        assert "oops" in log_line
        assert "step5_status" in log_line


# ---------------------------------------------------------------------------
# _count_jsonl_lines
# ---------------------------------------------------------------------------

class TestCountJsonlLines:

    def test_none_filepath(self):
        assert _count_jsonl_lines(None) == 0

    def test_empty_string_filepath(self):
        assert _count_jsonl_lines("") == 0

    def test_nonexistent_file(self):
        assert _count_jsonl_lines("/nonexistent/path/file.jsonl") == 0

    @pytest.mark.parametrize("n", [1, 2, 5, 10, 50, 100])
    def test_file_with_n_lines(self, tmp_dir, n):
        p = tmp_dir / "data.jsonl"
        p.write_text("".join(f'{{"i": {i}}}\n' for i in range(n)))
        assert _count_jsonl_lines(str(p)) == n

    def test_empty_file(self, empty_file):
        assert _count_jsonl_lines(empty_file()) == 0

    def test_file_with_blank_lines(self, tmp_dir):
        p = tmp_dir / "blank.jsonl"
        p.write_text("line1\n\nline3\n\n")
        assert _count_jsonl_lines(str(p)) == 4

    def test_no_trailing_newline(self, tmp_dir):
        p = tmp_dir / "no_nl.jsonl"
        p.write_text('{"a":1}\n{"b":2}')
        assert _count_jsonl_lines(str(p)) == 2

    def test_trailing_newline(self, tmp_dir):
        p = tmp_dir / "with_nl.jsonl"
        p.write_text('{"a":1}\n{"b":2}\n')
        assert _count_jsonl_lines(str(p)) == 2

    def test_single_line_no_newline(self, tmp_dir):
        p = tmp_dir / "single.jsonl"
        p.write_text('{"x":1}')
        assert _count_jsonl_lines(str(p)) == 1

    def test_only_newlines(self, tmp_dir):
        p = tmp_dir / "newlines.jsonl"
        p.write_text("\n\n\n")
        assert _count_jsonl_lines(str(p)) == 3


# ---------------------------------------------------------------------------
# _validate_step_output
# ---------------------------------------------------------------------------

class TestValidateStepOutput:

    def test_none_filepath_raises(self):
        with pytest.raises(AuroraPipelineError, match="missing"):
            _validate_step_output(None, 1)

    def test_empty_filepath_raises(self):
        with pytest.raises(AuroraPipelineError, match="missing"):
            _validate_step_output("", 2)

    def test_nonexistent_file_raises(self):
        with pytest.raises(AuroraPipelineError, match="missing"):
            _validate_step_output("/no/such/file.jsonl", 3)

    def test_empty_file_raises(self, empty_file):
        with pytest.raises(AuroraPipelineError, match="empty"):
            _validate_step_output(empty_file(), 4)

    @pytest.mark.parametrize("step_num", [1, 2, 3, 4, 5, 6])
    def test_valid_single_line(self, sample_jsonl_file, step_num):
        path = sample_jsonl_file([{"ok": True}])
        _validate_step_output(path, step_num)

    def test_valid_multi_line(self, sample_jsonl_file):
        path = sample_jsonl_file([{"a": 1}, {"b": 2}, {"c": 3}])
        _validate_step_output(path, 1)

    @pytest.mark.parametrize("bad_json,label", [
        ("{truncated", "truncated"),
        ("not json at all", "random_text"),
        ("[missing bracket", "missing_bracket"),
        ('{"key": }', "missing_value"),
        ("", "empty_line_only"),
    ])
    def test_invalid_json_line1_raises(self, tmp_dir, bad_json, label):
        p = tmp_dir / f"bad_{label}.jsonl"
        if bad_json == "":
            p.write_text("\n")
        else:
            p.write_text(bad_json + "\n")
        if bad_json == "":
            _validate_step_output(str(p), 1)
        else:
            with pytest.raises(AuroraPipelineError, match="invalid JSONL"):
                _validate_step_output(str(p), 1)

    @pytest.mark.parametrize("step_num", [1, 2, 3, 4, 5, 6])
    def test_step_num_in_error_message(self, tmp_dir, step_num):
        p = tmp_dir / f"bad_s{step_num}.jsonl"
        p.write_text("{bad json\n")
        with pytest.raises(AuroraPipelineError, match=f"Step {step_num}"):
            _validate_step_output(str(p), step_num)

    def test_valid_json_first_line_bad_second(self, tmp_dir):
        p = tmp_dir / "mixed.jsonl"
        p.write_text('{"ok":true}\nnot json\n')
        _validate_step_output(str(p), 1)

    def test_step_num_in_missing_error(self):
        with pytest.raises(AuroraPipelineError, match="Step 5"):
            _validate_step_output("/no/file", 5)

    def test_step_num_in_empty_error(self, empty_file):
        with pytest.raises(AuroraPipelineError, match="Step 6"):
            _validate_step_output(empty_file(), 6)

    @pytest.mark.parametrize("content", [
        '{"nested": {"a": [1,2,3]}}\n',
        '[1, 2, 3]\n',
        '"just a string"\n',
        '42\n',
        'true\n',
        'null\n',
    ])
    def test_various_valid_json_types(self, tmp_dir, content):
        p = tmp_dir / "typed.jsonl"
        p.write_text(content)
        _validate_step_output(str(p), 1)
