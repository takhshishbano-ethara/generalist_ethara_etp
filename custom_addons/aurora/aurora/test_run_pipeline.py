import hashlib
import sys
import types
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

sys.modules["odoo"].SUPERUSER_ID = 1

import aurora.worker.run_pipeline as rp


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_exc(cls, pgcode=None):
    exc = cls("boom")
    if pgcode is not None:
        exc.pgcode = pgcode
    return exc


class _FakeOperationalError(Exception):
    pass

_FakeOperationalError.__name__ = "OperationalError"


class _FakeIntegrityError(Exception):
    pass

_FakeIntegrityError.__name__ = "IntegrityError"


class _FakeProgrammingError(Exception):
    pass

_FakeProgrammingError.__name__ = "ProgrammingError"


@pytest.fixture(autouse=True)
def _reset_cancelled():
    rp._cancelled = False
    yield
    rp._cancelled = False


# ===================================================================
# a) _is_transient_db_error
# ===================================================================

@pytest.mark.parametrize("pgcode", ["40001", "40P01", "08006", "08001"])
def test_transient_operational_error_true(pgcode):
    exc = _make_exc(_FakeOperationalError, pgcode)
    assert rp._is_transient_db_error(exc) is True


@pytest.mark.parametrize("pgcode", ["40001", "40P01", "08006", "08001"])
def test_transient_generic_exception_with_pgcode(pgcode):
    exc = _make_exc(Exception, pgcode)
    assert rp._is_transient_db_error(exc) is True


@pytest.mark.parametrize("pgcode", ["23505", "42P01", "42601", "22P02", "23503", "42000"])
def test_non_transient_pgcodes(pgcode):
    exc = _make_exc(_FakeOperationalError, pgcode)
    assert rp._is_transient_db_error(exc) is False


@pytest.mark.parametrize("pgcode", ["23505", "42P01", "42601", "22P02"])
def test_non_transient_pgcodes_integrity(pgcode):
    exc = _make_exc(_FakeIntegrityError, pgcode)
    assert rp._is_transient_db_error(exc) is False


def test_operational_error_pgcode_none():
    exc = _FakeOperationalError("connection reset")
    exc.pgcode = None
    assert rp._is_transient_db_error(exc) is True


def test_operational_error_no_pgcode_attr():
    exc = _FakeOperationalError("oops")
    assert rp._is_transient_db_error(exc) is True


@pytest.mark.parametrize("exc_cls", [ValueError, RuntimeError, TypeError, KeyError, IOError])
def test_non_db_exception_false(exc_cls):
    exc = exc_cls("nope")
    assert rp._is_transient_db_error(exc) is False


def test_base_exception_no_pgcode():
    exc = Exception("generic")
    assert rp._is_transient_db_error(exc) is False


def test_integrity_error_pgcode_none():
    exc = _FakeIntegrityError("dup")
    exc.pgcode = None
    assert rp._is_transient_db_error(exc) is False


def test_programming_error_pgcode_none():
    exc = _FakeProgrammingError("syntax")
    exc.pgcode = None
    assert rp._is_transient_db_error(exc) is False


@pytest.mark.parametrize("pgcode", ["40001", "40P01"])
def test_transient_programming_error_with_transient_code(pgcode):
    exc = _make_exc(_FakeProgrammingError, pgcode)
    assert rp._is_transient_db_error(exc) is True


def test_exception_with_empty_pgcode():
    exc = Exception("x")
    exc.pgcode = ""
    assert rp._is_transient_db_error(exc) is False


@pytest.mark.parametrize("pgcode", ["99999", "XXXXX", "00000"])
def test_unknown_pgcodes(pgcode):
    exc = _make_exc(Exception, pgcode)
    assert rp._is_transient_db_error(exc) is False


# ===================================================================
# b) _open_cursor
# ===================================================================

def test_open_cursor_calls_registry(mock_registry, mock_cursor):
    result = rp._open_cursor(mock_registry)
    mock_registry.cursor.assert_called_once()
    assert result is mock_cursor


def test_open_cursor_returns_cursor_object(mock_registry, mock_cursor):
    cr = rp._open_cursor(mock_registry)
    assert cr is mock_cursor


# ===================================================================
# c) _notify_bus
# ===================================================================

STEP_STAGES = [
    "fetch_prs", "filter_prs", "discover_tags", "group_prs",
    "fetch_issues", "build_dataset", "phase2_build", "phase2_report",
    "done", "failed", "idle", "queued", "running", "cancelled",
]


@pytest.mark.parametrize("stage", STEP_STAGES)
def test_notify_bus_stages(mock_registry, mock_cursor, stage):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 42, stage, "hello")

    mock_cursor.commit.assert_called_once()
    mock_cursor.close.assert_called_once()


@pytest.mark.parametrize("rec_id", [1, 100, 999999])
def test_notify_bus_rec_ids(mock_registry, mock_cursor, rec_id):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", rec_id, "running", None)

    bus_model._sendone.assert_called_once()
    call_args = bus_model._sendone.call_args
    assert str(rec_id) in call_args[0][0]


def test_notify_bus_progress_none(mock_registry, mock_cursor):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 1, "done", None)

    data = bus_model._sendone.call_args[0][2]
    assert data["progress_text"] == ""


def test_notify_bus_progress_string(mock_registry, mock_cursor):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 1, "done", "All done!")

    data = bus_model._sendone.call_args[0][2]
    assert data["progress_text"] == "All done!"


def test_notify_bus_exception_swallowed(mock_registry, mock_cursor):
    with patch("odoo.api.Environment", side_effect=RuntimeError("fail")):
        rp._notify_bus(mock_registry, "testdb", 1, "done")

    mock_cursor.close.assert_called_once()


def test_notify_bus_channel_format(mock_registry, mock_cursor):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 77, "fetch_prs")

    channel = bus_model._sendone.call_args[0][0]
    assert channel == "aurora_pipeline_77"


def test_notify_bus_type_field(mock_registry, mock_cursor):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 1, "running")

    msg_type = bus_model._sendone.call_args[0][1]
    assert msg_type == "aurora_pipeline_update"


def test_notify_bus_data_pipeline_id(mock_registry, mock_cursor):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 55, "done", "fin")

    data = bus_model._sendone.call_args[0][2]
    assert data["pipeline_id"] == 55
    assert data["stage"] == "done"
    assert data["progress_text"] == "fin"


def test_notify_bus_cursor_closed_on_success(mock_registry, mock_cursor):
    mock_env = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock())

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 1, "done")

    mock_cursor.close.assert_called_once()


# ===================================================================
# d) _post_chatter
# ===================================================================

@pytest.mark.parametrize("body", [
    "Pipeline complete",
    "Step 1 failed: timeout",
    "",
    "Special chars: <b>bold</b> & 'quotes'",
    "A" * 1000,
])
def test_post_chatter_bodies(mock_registry, mock_cursor, body):
    mock_env = MagicMock()
    rec = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=rec)))

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._post_chatter(mock_registry, 2, 10, body)

    rec.message_post.assert_called_once_with(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
    mock_cursor.commit.assert_called_once()
    mock_cursor.close.assert_called_once()


def test_post_chatter_uid_none_uses_superuser(mock_registry, mock_cursor):
    mock_env = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=MagicMock())))

    with patch("odoo.api.Environment", return_value=mock_env) as env_cls:
        rp._post_chatter(mock_registry, None, 10, "test")

    call_args = env_cls.call_args
    assert call_args[0][1] == 1  # SUPERUSER_ID


def test_post_chatter_uid_provided(mock_registry, mock_cursor):
    mock_env = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=MagicMock())))

    with patch("odoo.api.Environment", return_value=mock_env) as env_cls:
        rp._post_chatter(mock_registry, 42, 10, "test")

    call_args = env_cls.call_args
    assert call_args[0][1] == 42


def test_post_chatter_exception_swallowed(mock_registry, mock_cursor):
    with patch("odoo.api.Environment", side_effect=RuntimeError("fail")):
        rp._post_chatter(mock_registry, 1, 10, "test")

    mock_cursor.close.assert_called_once()


def test_post_chatter_cursor_closed_on_exc_in_message_post(mock_registry, mock_cursor):
    mock_env = MagicMock()
    rec = MagicMock()
    rec.message_post.side_effect = RuntimeError("post fail")
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=rec)))

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._post_chatter(mock_registry, 1, 10, "test")

    mock_cursor.close.assert_called_once()


@pytest.mark.parametrize("rec_id", [1, 50, 99999])
def test_post_chatter_browses_correct_rec(mock_registry, mock_cursor, rec_id):
    mock_env = MagicMock()
    pipeline_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=pipeline_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._post_chatter(mock_registry, 1, rec_id, "msg")

    pipeline_model.browse.assert_called_once_with(rec_id)


# ===================================================================
# e) _create_phase2_results
# ===================================================================

def test_create_phase2_empty_results(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [])

    result_model.create.assert_not_called()
    mock_cursor.commit.assert_called_once()


def test_create_phase2_unlinks_old_results(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    search_result = MagicMock()
    result_model.search.return_value = search_result
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [])

    result_model.search.assert_called_once_with([("pipeline_id", "=", 10)])
    search_result.unlink.assert_called_once()


def test_create_phase2_single_full_result(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    results = [{
        "instance_id": "inst_1",
        "valid": True,
        "f2p": ["t1", "t2"],
        "p2p": ["t3"],
        "s2p": [],
        "n2p": ["t4"],
        "fixed_tests": ["t5"],
        "error_msg": "",
    }]

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, results)

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["pipeline_id"] == 10
    assert call_vals["sequence"] == 1
    assert call_vals["instance_id"] == "inst_1"
    assert call_vals["valid"] is True
    assert call_vals["f2p_count"] == 2
    assert call_vals["p2p_count"] == 1
    assert call_vals["s2p_count"] == 0
    assert call_vals["n2p_count"] == 1
    assert call_vals["fixed_count"] == 1


def test_create_phase2_missing_f2p(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"instance_id": "x"}])

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["f2p_count"] == 0
    assert call_vals["f2p_tests"] == ""


def test_create_phase2_missing_p2p(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"valid": True}])

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["p2p_count"] == 0
    assert call_vals["p2p_tests"] == ""


def test_create_phase2_missing_s2p(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{}])

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["s2p_count"] == 0


def test_create_phase2_missing_n2p(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{}])

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["n2p_count"] == 0


def test_create_phase2_missing_fixed(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{}])

    call_vals = result_model.create.call_args[0][0]
    assert call_vals["fixed_count"] == 0


@pytest.mark.parametrize("count", [2, 5, 10])
def test_create_phase2_multiple_results(mock_registry, mock_cursor, count):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    results = [{"instance_id": f"i{i}", "valid": True} for i in range(count)]

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, results)

    assert result_model.create.call_count == count


def test_create_phase2_sequence_increments(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    results = [{"instance_id": f"i{i}"} for i in range(3)]

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, results)

    seqs = [c[0][0]["sequence"] for c in result_model.create.call_args_list]
    assert seqs == [1, 2, 3]


def test_create_phase2_error_msg_field(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"error_msg": "build failed"}])

    assert result_model.create.call_args[0][0]["error_msg"] == "build failed"


def test_create_phase2_error_fallback(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"error": "alt error"}])

    assert result_model.create.call_args[0][0]["error_msg"] == "alt error"


def test_create_phase2_error_msg_precedence(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"error_msg": "primary", "error": "fallback"}])

    assert result_model.create.call_args[0][0]["error_msg"] == "primary"


def test_create_phase2_no_error_fields(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"valid": True}])

    assert result_model.create.call_args[0][0]["error_msg"] == ""


def test_create_phase2_exception_rollback(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    result_model.search.side_effect = RuntimeError("db fail")
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"valid": True}])

    mock_cursor.rollback.assert_called_once()
    mock_cursor.close.assert_called_once()


def test_create_phase2_exception_cursor_closed(mock_registry, mock_cursor):
    with patch("odoo.api.Environment", side_effect=RuntimeError("nope")):
        rp._create_phase2_results(mock_registry, 10, [])

    mock_cursor.close.assert_called_once()


def test_create_phase2_f2p_tests_joined(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"f2p": ["a", "b", "c"]}])

    assert result_model.create.call_args[0][0]["f2p_tests"] == "a\nb\nc"


def test_create_phase2_valid_default_false(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{}])

    assert result_model.create.call_args[0][0]["valid"] is False


def test_create_phase2_instance_id_default(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{}])

    assert result_model.create.call_args[0][0]["instance_id"] == ""


@pytest.mark.parametrize("rec_id", [1, 500, 99999])
def test_create_phase2_pipeline_id_set(mock_registry, mock_cursor, rec_id):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, rec_id, [{"valid": True}])

    assert result_model.create.call_args[0][0]["pipeline_id"] == rec_id


# ===================================================================
# f) _build_s3_config
# ===================================================================

def test_build_s3_config_basic():
    cfg = {"s3_bucket": "b", "s3_access_key": "ak", "s3_secret_key": "sk", "s3_region": "us-east-1"}
    result = rp._build_s3_config(cfg)
    assert result == {"bucket": "b", "access_key": "ak", "secret_key": "sk", "region": "us-east-1"}


@pytest.mark.parametrize("bucket,ak,sk,region", [
    ("my-bucket", "AKIA123", "secret", "eu-west-1"),
    ("", "", "", "ap-south-1"),
    ("prod-bucket", "key", "skey", "us-west-2"),
    ("test", "a", "b", ""),
])
def test_build_s3_config_parametrized(bucket, ak, sk, region):
    cfg = {"s3_bucket": bucket, "s3_access_key": ak, "s3_secret_key": sk, "s3_region": region}
    result = rp._build_s3_config(cfg)
    assert result["bucket"] == bucket
    assert result["access_key"] == ak
    assert result["secret_key"] == sk
    assert result["region"] == region


def test_build_s3_config_missing_key():
    with pytest.raises(KeyError):
        rp._build_s3_config({"s3_bucket": "b"})


def test_build_s3_config_extra_keys_ignored():
    cfg = {"s3_bucket": "b", "s3_access_key": "a", "s3_secret_key": "s", "s3_region": "r", "extra": "x"}
    result = rp._build_s3_config(cfg)
    assert "extra" not in result


# ===================================================================
# g) PipelineCancelled
# ===================================================================

def test_pipeline_cancelled_is_exception():
    assert issubclass(rp.PipelineCancelled, Exception)


def test_pipeline_cancelled_message():
    exc = rp.PipelineCancelled("stopped")
    assert str(exc) == "stopped"


def test_pipeline_cancelled_empty():
    exc = rp.PipelineCancelled()
    assert str(exc) == ""


def test_pipeline_cancelled_raise_catch():
    with pytest.raises(rp.PipelineCancelled):
        raise rp.PipelineCancelled("test")


# ===================================================================
# h) _sigterm_handler and _check_cancelled
# ===================================================================

def test_sigterm_handler_sets_cancelled():
    rp._cancelled = False
    rp._sigterm_handler(15, None)
    assert rp._cancelled is True


def test_check_cancelled_when_false():
    rp._cancelled = False
    rp._check_cancelled()


def test_check_cancelled_when_true():
    rp._cancelled = True
    with pytest.raises(rp.PipelineCancelled):
        rp._check_cancelled()


def test_check_cancelled_message():
    rp._cancelled = True
    with pytest.raises(rp.PipelineCancelled, match="SIGTERM"):
        rp._check_cancelled()


def test_sigterm_then_check():
    rp._cancelled = False
    rp._sigterm_handler(15, None)
    with pytest.raises(rp.PipelineCancelled):
        rp._check_cancelled()


def test_check_cancelled_reset():
    rp._cancelled = True
    rp._cancelled = False
    rp._check_cancelled()


# ===================================================================
# i) _read_config
# ===================================================================

def _setup_read_config_mocks(mock_registry, mock_cursor, pipeline_exists=True,
                              icp_overrides=None, pipeline_attrs=None):
    mock_env = MagicMock()

    pipeline = MagicMock()
    pipeline.exists.return_value = pipeline_exists
    pipeline.github_org = "myorg"
    pipeline.github_repo = "myrepo"
    pipeline.output_dir = "/out"
    pipeline.skip_pr_fetch = False
    pipeline.detected_lang = "python"
    pipeline.user_id.id = 5

    if pipeline_attrs:
        for k, v in pipeline_attrs.items():
            setattr(pipeline, k, v)

    icp = MagicMock()
    defaults = {
        "aurora.lang": "python",
        "aurora.delay_on_error": "300",
        "aurora.retry_attempts": "3",
        "aurora.max_tags": "200",
        "aurora.window_days": "30",
        "aurora.cache_dir": "/data/repo_cache",
        "aurora.s3_bucket": "",
        "aurora.s3_region": "ap-south-1",
        "aurora.s3_folder": "",
    }
    if icp_overrides:
        defaults.update(icp_overrides)

    def icp_get_param(key, default=""):
        return defaults.get(key, default)

    icp.get_param = icp_get_param

    def env_getitem(self_or_key, model_name=None):
        if model_name is None:
            model_name = self_or_key
        if model_name == "aurora.pipeline":
            return MagicMock(browse=MagicMock(return_value=pipeline))
        elif model_name == "ir.config_parameter":
            return MagicMock(sudo=MagicMock(return_value=icp))
        return MagicMock()

    mock_env.__getitem__ = env_getitem

    get_encrypted = MagicMock(return_value="encrypted_val")

    class FakeError(Exception):
        pass

    return mock_env, get_encrypted, FakeError


def test_read_config_returns_dict(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(mock_registry, mock_cursor)

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert isinstance(result, dict)
    assert "org" in result
    assert "repo" in result


def test_read_config_all_keys(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(mock_registry, mock_cursor)

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    expected_keys = {
        "org", "repo", "output_dir", "skip_pr_fetch", "lang",
        "delay_on_error", "retry_attempts", "max_tags", "window_days",
        "cache_dir", "s3_bucket", "s3_access_key", "s3_secret_key",
        "s3_region", "s3_folder", "uid",
    }
    assert set(result.keys()) == expected_keys


def test_read_config_pipeline_not_found(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor, pipeline_exists=False)

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        with pytest.raises(Exception):
            rp._read_config(mock_registry, 999)


def test_read_config_uses_encrypted_params(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(mock_registry, mock_cursor)

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result["s3_access_key"] == "encrypted_val"
    assert result["s3_secret_key"] == "encrypted_val"
    assert get_enc.call_count == 2


def test_read_config_cursor_closed(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(mock_registry, mock_cursor)

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        rp._read_config(mock_registry, 1)

    mock_cursor.close.assert_called_once()


@pytest.mark.parametrize("icp_key,icp_val,cfg_key,expected", [
    ("aurora.delay_on_error", "600", "delay_on_error", 600),
    ("aurora.retry_attempts", "5", "retry_attempts", 5),
    ("aurora.max_tags", "100", "max_tags", 100),
    ("aurora.window_days", "60", "window_days", 60),
    ("aurora.cache_dir", "/custom/cache", "cache_dir", "/custom/cache"),
    ("aurora.s3_bucket", "my-bucket", "s3_bucket", "my-bucket"),
    ("aurora.s3_region", "eu-west-1", "s3_region", "eu-west-1"),
    ("aurora.s3_folder", "output", "s3_folder", "output"),
])
def test_read_config_icp_overrides(mock_registry, mock_cursor, icp_key, icp_val, cfg_key, expected):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor, icp_overrides={icp_key: icp_val})

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result[cfg_key] == expected


def test_read_config_detected_lang_override(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor, pipeline_attrs={"detected_lang": "java"})

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result["lang"] == "java"


def test_read_config_detected_lang_none_fallback(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor,
        pipeline_attrs={"detected_lang": None},
        icp_overrides={"aurora.lang": "go"})

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result["lang"] == "go"


def test_read_config_detected_lang_empty_fallback(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor,
        pipeline_attrs={"detected_lang": ""},
        icp_overrides={"aurora.lang": "rust"})

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result["lang"] == "rust"


def test_read_config_uid_from_pipeline(mock_registry, mock_cursor):
    mock_env, get_enc, FakeErr = _setup_read_config_mocks(
        mock_registry, mock_cursor, pipeline_attrs={"user_id": MagicMock(id=42)})

    with patch("odoo.api.Environment", return_value=mock_env), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.credential_manager": MagicMock(get_encrypted_param_raw=get_enc),
             "odoo.addons.aurora.tools.util": MagicMock(AuroraPipelineError=FakeErr),
         }):
        result = rp._read_config(mock_registry, 1)

    assert result["uid"] == 42


# ===================================================================
# j) _lease_tokens / _release_tokens
# ===================================================================

def test_lease_tokens_calls_lease(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    mock_token_cls.lease_tokens.return_value = ["tok1", "tok2"]

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        result = rp._lease_tokens(mock_registry, 10, count=2)

    mock_token_cls.lease_tokens.assert_called_once_with(mock_cursor, 10, count=2)
    assert result == ["tok1", "tok2"]


def test_lease_tokens_commits(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    mock_token_cls.lease_tokens.return_value = []

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._lease_tokens(mock_registry, 10)

    mock_cursor.commit.assert_called_once()


def test_lease_tokens_cursor_closed(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    mock_token_cls.lease_tokens.return_value = []

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._lease_tokens(mock_registry, 10)

    mock_cursor.close.assert_called_once()


def test_lease_tokens_empty(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    mock_token_cls.lease_tokens.return_value = []

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        result = rp._lease_tokens(mock_registry, 10)

    assert result == []


@pytest.mark.parametrize("count", [1, 3, 5, 10])
def test_lease_tokens_count_param(mock_registry, mock_cursor, count):
    mock_token_cls = MagicMock()
    mock_token_cls.lease_tokens.return_value = [f"t{i}" for i in range(count)]

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        result = rp._lease_tokens(mock_registry, 10, count=count)

    assert len(result) == count


def test_release_tokens_calls_release(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._release_tokens(mock_registry, 10, {"h": {"remaining": 50}})

    mock_token_cls.release_tokens.assert_called_once_with(mock_cursor, 10, {"h": {"remaining": 50}})


def test_release_tokens_commits(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._release_tokens(mock_registry, 10)

    mock_cursor.commit.assert_called_once()


def test_release_tokens_cursor_closed(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._release_tokens(mock_registry, 10)

    mock_cursor.close.assert_called_once()


def test_release_tokens_exception_swallowed(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    mock_token_cls.release_tokens.side_effect = RuntimeError("fail")

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._release_tokens(mock_registry, 10)

    mock_cursor.close.assert_called_once()


def test_release_tokens_none_summaries(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch.dict("sys.modules", {
        "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
    }):
        rp._release_tokens(mock_registry, 10, None)

    mock_token_cls.release_tokens.assert_called_once_with(mock_cursor, 10, None)


# ===================================================================
# k) _heartbeat_rate_limits
# ===================================================================

def test_heartbeat_rate_limits_zero_tokens(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch("requests.get") as mock_get, \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, [])

    mock_token_cls.heartbeat_rate_limits.assert_not_called()


def test_heartbeat_rate_limits_one_token_200(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 4999, "reset": 12345}}}

    with patch("requests.get", return_value=resp), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["ghp_abc"])

    mock_token_cls.heartbeat_rate_limits.assert_called_once()
    call_args = mock_token_cls.heartbeat_rate_limits.call_args
    summaries = call_args[0][2]
    tok_hash = hashlib.sha256(b"ghp_abc").hexdigest()
    assert tok_hash in summaries
    assert summaries[tok_hash]["remaining"] == 4999


@pytest.mark.parametrize("n_tokens", [1, 3])
def test_heartbeat_rate_limits_n_tokens(mock_registry, mock_cursor, n_tokens):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 100, "reset": 99}}}
    tokens = [f"tok_{i}" for i in range(n_tokens)]

    with patch("requests.get", return_value=resp), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, tokens)

    call_args = mock_token_cls.heartbeat_rate_limits.call_args
    summaries = call_args[0][2]
    assert len(summaries) == n_tokens


def test_heartbeat_rate_limits_non_200(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 403
    resp.json.return_value = {}

    with patch("requests.get", return_value=resp), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    mock_token_cls.heartbeat_rate_limits.assert_not_called()


def test_heartbeat_rate_limits_timeout(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    import requests as req_mod

    with patch("requests.get", side_effect=req_mod.exceptions.Timeout("timeout")), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    mock_token_cls.heartbeat_rate_limits.assert_not_called()


def test_heartbeat_rate_limits_connection_error(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    with patch("requests.get", side_effect=ConnectionError("refused")), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    mock_token_cls.heartbeat_rate_limits.assert_not_called()


def test_heartbeat_rate_limits_cursor_closed(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 1, "reset": 2}}}

    with patch("requests.get", return_value=resp), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    mock_cursor.close.assert_called()


def test_heartbeat_rate_limits_auth_header(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 0, "reset": 0}}}

    with patch("requests.get", return_value=resp) as mock_get, \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["ghp_xyz"])

    call_kwargs = mock_get.call_args
    assert call_kwargs[1]["headers"]["Authorization"] == "Bearer ghp_xyz"


def test_heartbeat_rate_limits_mixed_success_fail(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {"resources": {"core": {"remaining": 50, "reset": 1}}}

    resp_fail = MagicMock()
    resp_fail.status_code = 401

    with patch("requests.get", side_effect=[resp_ok, resp_fail, resp_ok]), \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["t1", "t2", "t3"])

    summaries = mock_token_cls.heartbeat_rate_limits.call_args[0][2]
    assert len(summaries) == 2


# ===================================================================
# l) main() function
# ===================================================================

def test_main_missing_pipeline_id():
    with patch.dict("os.environ", {}, clear=True), \
         pytest.raises(SystemExit) as exc_info:
        rp.main()
    assert exc_info.value.code == 1


def test_main_missing_odoo_db():
    with patch.dict("os.environ", {"PIPELINE_ID": "1"}, clear=True), \
         pytest.raises(SystemExit) as exc_info:
        rp.main()
    assert exc_info.value.code == 1


@pytest.mark.parametrize("val", ["abc", "1.5", "", "not-a-number", "12x"])
def test_main_non_integer_pipeline_id(val):
    with patch.dict("os.environ", {"PIPELINE_ID": val, "ODOO_DB": "testdb"}, clear=True), \
         pytest.raises(SystemExit) as exc_info:
        rp.main()
    assert exc_info.value.code == 1


def test_main_boot_odoo_failure():
    with patch.dict("os.environ", {"PIPELINE_ID": "1", "ODOO_DB": "testdb"}, clear=True), \
         patch.object(rp, "_boot_odoo", side_effect=RuntimeError("boom")), \
         pytest.raises(SystemExit) as exc_info:
        rp.main()
    assert exc_info.value.code == 2


def test_main_valid_env_calls_run_pipeline():
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": "42", "ODOO_DB": "mydb"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg), \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline") as mock_run, \
         pytest.raises(SystemExit) as exc_info:
        rp.main()

    mock_run.assert_called_once_with(mock_reg, "mydb", 42)
    assert exc_info.value.code == 0


def test_main_run_pipeline_exception():
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": "1", "ODOO_DB": "db"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg), \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline", side_effect=RuntimeError("fatal")), \
         pytest.raises(SystemExit) as exc_info:
        rp.main()

    assert exc_info.value.code == 3


def test_main_odoo_conf_env():
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": "1", "ODOO_DB": "db", "ODOO_CONF": "/etc/custom.conf"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg) as mock_boot, \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline"), \
         pytest.raises(SystemExit):
        rp.main()

    mock_boot.assert_called_once_with("db", "/etc/custom.conf")


# ===================================================================
# Additional parametrized edge cases for thorough coverage
# ===================================================================

@pytest.mark.parametrize("pgcode,exc_cls,expected", [
    ("40001", _FakeOperationalError, True),
    ("40P01", _FakeOperationalError, True),
    ("08006", _FakeOperationalError, True),
    ("08001", _FakeOperationalError, True),
    ("40001", _FakeIntegrityError, True),
    ("23505", _FakeOperationalError, False),
    ("42P01", _FakeIntegrityError, False),
    (None, _FakeOperationalError, True),
    (None, _FakeIntegrityError, False),
    (None, _FakeProgrammingError, False),
    ("40001", Exception, True),
    ("99999", Exception, False),
    (None, ValueError, False),
    (None, RuntimeError, False),
])
def test_is_transient_matrix(pgcode, exc_cls, expected):
    exc = exc_cls("err")
    if pgcode is not None:
        exc.pgcode = pgcode
    assert rp._is_transient_db_error(exc) is expected


@pytest.mark.parametrize("body", [
    "Pipeline complete - org/repo (100 records)",
    "Pipeline failed:\nTraceback...",
    "Pipeline stopped (SIGTERM received).",
    "<p>HTML body</p>",
])
def test_post_chatter_various_bodies(mock_registry, mock_cursor, body):
    mock_env = MagicMock()
    rec = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=rec)))

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._post_chatter(mock_registry, 1, 10, body)

    rec.message_post.assert_called_once()
    assert rec.message_post.call_args[1]["body"] == body


@pytest.mark.parametrize("results,expected_count", [
    ([], 0),
    ([{"valid": True}], 1),
    ([{"valid": True}, {"valid": False}], 2),
    ([{"valid": True}] * 7, 7),
])
def test_create_phase2_result_count(mock_registry, mock_cursor, results, expected_count):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, results)

    assert result_model.create.call_count == expected_count


@pytest.mark.parametrize("f2p,p2p,s2p,n2p,fixed", [
    ([], [], [], [], []),
    (["a"], [], [], [], []),
    ([], ["b"], [], [], []),
    ([], [], ["c"], [], []),
    ([], [], [], ["d"], []),
    ([], [], [], [], ["e"]),
    (["a", "b"], ["c"], ["d", "e", "f"], ["g"], ["h", "i"]),
])
def test_create_phase2_counts(mock_registry, mock_cursor, f2p, p2p, s2p, n2p, fixed):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    results = [{"f2p": f2p, "p2p": p2p, "s2p": s2p, "n2p": n2p, "fixed_tests": fixed}]

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, results)

    vals = result_model.create.call_args[0][0]
    assert vals["f2p_count"] == len(f2p)
    assert vals["p2p_count"] == len(p2p)
    assert vals["s2p_count"] == len(s2p)
    assert vals["n2p_count"] == len(n2p)
    assert vals["fixed_count"] == len(fixed)


@pytest.mark.parametrize("stage,text", [
    ("fetch_prs", None),
    ("filter_prs", "Step 2"),
    ("done", "All done"),
    ("failed", "Error occurred"),
])
def test_notify_bus_stage_text_combos(mock_registry, mock_cursor, stage, text):
    mock_env = MagicMock()
    bus_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=bus_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._notify_bus(mock_registry, "testdb", 1, stage, text)

    data = bus_model._sendone.call_args[0][2]
    assert data["stage"] == stage
    assert data["progress_text"] == (text or "")


def test_transient_pg_codes_frozenset():
    assert isinstance(rp._TRANSIENT_PG_CODES, frozenset)
    assert len(rp._TRANSIENT_PG_CODES) == 4


def test_db_write_max_retries_constant():
    assert rp._DB_WRITE_MAX_RETRIES == 3


def test_db_write_retry_base_delay_constant():
    assert rp._DB_WRITE_RETRY_BASE_DELAY == 0.5


@pytest.mark.parametrize("pipeline_id_str", ["1", "100", "999999"])
def test_main_integer_conversion(pipeline_id_str):
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": pipeline_id_str, "ODOO_DB": "db"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg), \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline") as mock_run, \
         pytest.raises(SystemExit):
        rp.main()

    assert mock_run.call_args[0][2] == int(pipeline_id_str)


def test_pipeline_cancelled_inheritance():
    exc = rp.PipelineCancelled("test")
    assert isinstance(exc, Exception)
    assert isinstance(exc, rp.PipelineCancelled)


def test_sigterm_handler_idempotent():
    rp._cancelled = False
    rp._sigterm_handler(15, None)
    rp._sigterm_handler(15, None)
    assert rp._cancelled is True


def test_build_s3_config_returns_four_keys():
    cfg = {"s3_bucket": "b", "s3_access_key": "a", "s3_secret_key": "s", "s3_region": "r"}
    result = rp._build_s3_config(cfg)
    assert len(result) == 4


@pytest.mark.parametrize("uid_in,expected_uid", [(None, 1), (0, 1), (5, 5), (100, 100)])
def test_post_chatter_uid_logic(mock_registry, mock_cursor, uid_in, expected_uid):
    mock_env = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=MagicMock(browse=MagicMock(return_value=MagicMock())))

    with patch("odoo.api.Environment", return_value=mock_env) as env_cls:
        rp._post_chatter(mock_registry, uid_in, 10, "test")

    actual_uid = env_cls.call_args[0][1]
    assert actual_uid == expected_uid


def test_heartbeat_rate_limits_url(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 0, "reset": 0}}}

    with patch("requests.get", return_value=resp) as mock_get, \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    assert mock_get.call_args[0][0] == "https://api.github.com/rate_limit"


def test_heartbeat_rate_limits_timeout_param(mock_registry, mock_cursor):
    mock_token_cls = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"resources": {"core": {"remaining": 0, "reset": 0}}}

    with patch("requests.get", return_value=resp) as mock_get, \
         patch.dict("sys.modules", {
             "odoo.addons.aurora.models.github_token": MagicMock(AuroraGithubToken=mock_token_cls),
         }):
        rp._heartbeat_rate_limits(mock_registry, 10, ["tok1"])

    assert mock_get.call_args[1]["timeout"] == 10


def test_create_phase2_p2p_tests_joined(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"p2p": ["x", "y"]}])

    assert result_model.create.call_args[0][0]["p2p_tests"] == "x\ny"


def test_create_phase2_s2p_tests_joined(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"s2p": ["s1"]}])

    assert result_model.create.call_args[0][0]["s2p_tests"] == "s1"


def test_create_phase2_n2p_tests_joined(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"n2p": ["n1", "n2"]}])

    assert result_model.create.call_args[0][0]["n2p_tests"] == "n1\nn2"


def test_create_phase2_fixed_tests_joined(mock_registry, mock_cursor):
    mock_env = MagicMock()
    result_model = MagicMock()
    mock_env.__getitem__ = MagicMock(return_value=result_model)

    with patch("odoo.api.Environment", return_value=mock_env):
        rp._create_phase2_results(mock_registry, 10, [{"fixed_tests": ["f1", "f2", "f3"]}])

    assert result_model.create.call_args[0][0]["fixed_tests"] == "f1\nf2\nf3"


def test_open_cursor_multiple_calls(mock_registry, mock_cursor):
    rp._open_cursor(mock_registry)
    rp._open_cursor(mock_registry)
    assert mock_registry.cursor.call_count == 2


def test_main_pipeline_id_zero():
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": "0", "ODOO_DB": "db"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg), \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline") as mock_run, \
         pytest.raises(SystemExit):
        rp.main()

    mock_run.assert_called_once_with(mock_reg, "db", 0)


def test_main_negative_pipeline_id():
    mock_reg = MagicMock()
    with patch.dict("os.environ", {"PIPELINE_ID": "-1", "ODOO_DB": "db"}, clear=True), \
         patch.object(rp, "_boot_odoo", return_value=mock_reg), \
         patch.object(rp, "_init_shared_functions"), \
         patch.object(rp, "run_pipeline") as mock_run, \
         pytest.raises(SystemExit):
        rp.main()

    mock_run.assert_called_once_with(mock_reg, "db", -1)
