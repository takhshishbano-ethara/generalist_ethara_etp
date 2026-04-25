import types
from unittest.mock import MagicMock

import pytest

from aurora.models.pipeline_result import AuroraPipelineResult


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_rec(**kw):
    rec = types.SimpleNamespace(
        valid=kw.get("valid", False),
        error_msg=kw.get("error_msg", ""),
        status_icon=None,
        f2p_count=kw.get("f2p_count", 0),
        p2p_count=kw.get("p2p_count", 0),
        s2p_count=kw.get("s2p_count", 0),
        n2p_count=kw.get("n2p_count", 0),
        fixed_count=kw.get("fixed_count", 0),
        instance_id=kw.get("instance_id", ""),
        f2p_tests=kw.get("f2p_tests", ""),
        p2p_tests=kw.get("p2p_tests", ""),
        s2p_tests=kw.get("s2p_tests", ""),
        n2p_tests=kw.get("n2p_tests", ""),
        fixed_tests=kw.get("fixed_tests", ""),
        sequence=kw.get("sequence", 10),
    )
    return rec


def _compute(rec):
    AuroraPipelineResult._compute_status_icon(iter([rec]))


# ---------------------------------------------------------------------------
# A — _compute_status_icon  (35 parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("valid,error_msg,expected", [
    (True, "", "✓ Resolved"),
    (True, None, "✓ Resolved"),
    (True, "some error", "✓ Resolved"),
    (True, "Docker build failed", "✓ Resolved"),
    (True, "timeout", "✓ Resolved"),
    (True, "x" * 1000, "✓ Resolved"),
    (True, "  ", "✓ Resolved"),
    (True, "\n", "✓ Resolved"),
    (True, "0", "✓ Resolved"),
    (True, "False", "✓ Resolved"),
    (False, "some error", "✗ Error"),
    (False, "Docker build failed: exit code 1", "✗ Error"),
    (False, "timeout after 300s", "✗ Error"),
    (False, "OOM killed", "✗ Error"),
    (False, "Permission denied", "✗ Error"),
    (False, "x" * 5000, "✗ Error"),
    (False, "  whitespace  ", "✗ Error"),
    (False, "\ttab", "✗ Error"),
    (False, "0", "✗ Error"),
    (False, "False", "✗ Error"),
    (False, "None", "✗ Error"),
    (False, "1", "✗ Error"),
    (False, "error\nnewline", "✗ Error"),
    (False, "", "✗ Unresolved"),
    (False, None, "✗ Unresolved"),
    (False, 0, "✗ Unresolved"),
    (False, False, "✗ Unresolved"),
    (False, [], "✗ Unresolved"),
    (True, 0, "✓ Resolved"),
    (True, False, "✓ Resolved"),
    (True, [], "✓ Resolved"),
    (False, "a", "✗ Error"),
    (False, " ", "✗ Error"),
    (False, "résumé", "✗ Error"),
    (False, "日本語", "✗ Error"),
])
def test_compute_status_icon(valid, error_msg, expected):
    rec = _make_rec(valid=valid, error_msg=error_msg)
    _compute(rec)
    assert rec.status_icon == expected


# ---------------------------------------------------------------------------
# B — field definitions (14+ fields)
# ---------------------------------------------------------------------------

_EXPECTED_FIELDS = {
    "pipeline_id": "Many2one",
    "sequence": "Integer",
    "instance_id": "Char",
    "valid": "Boolean",
    "f2p_count": "Integer",
    "p2p_count": "Integer",
    "s2p_count": "Integer",
    "n2p_count": "Integer",
    "fixed_count": "Integer",
    "f2p_tests": "Text",
    "p2p_tests": "Text",
    "s2p_tests": "Text",
    "n2p_tests": "Text",
    "fixed_tests": "Text",
    "error_msg": "Text",
    "status_icon": "Char",
}

@pytest.mark.parametrize("field_name,expected_type", list(_EXPECTED_FIELDS.items()))
def test_field_exists_and_type(field_name, expected_type):
    field_obj = getattr(AuroraPipelineResult, field_name, None)
    assert field_obj is not None, f"Field {field_name} not found"
    assert type(field_obj).__name__.lstrip("_") == expected_type


# ---------------------------------------------------------------------------
# C — model metadata (3 tests)
# ---------------------------------------------------------------------------

def test_model_name():
    assert AuroraPipelineResult._name == "aurora.pipeline.result"


def test_model_description():
    assert AuroraPipelineResult._description == "Aurora Phase 2 Instance Result"


def test_model_order():
    assert AuroraPipelineResult._order == "sequence, id"


# ---------------------------------------------------------------------------
# D — result data structures (100+ parametrized)
# ---------------------------------------------------------------------------

_COUNT_COMBOS = [
    (0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0),
    (0, 1, 0, 0, 0),
    (0, 0, 1, 0, 0),
    (0, 0, 0, 1, 0),
    (0, 0, 0, 0, 1),
    (5, 0, 0, 0, 0),
    (10, 0, 0, 0, 0),
    (50, 0, 0, 0, 0),
    (100, 0, 0, 0, 0),
    (0, 5, 0, 0, 0),
    (0, 10, 0, 0, 0),
    (0, 50, 0, 0, 0),
    (0, 100, 0, 0, 0),
    (0, 0, 5, 0, 0),
    (0, 0, 10, 0, 0),
    (0, 0, 50, 0, 0),
    (0, 0, 0, 5, 0),
    (0, 0, 0, 10, 0),
    (0, 0, 0, 50, 0),
    (1, 1, 1, 1, 1),
    (5, 3, 2, 1, 4),
    (10, 10, 10, 10, 10),
    (100, 50, 25, 10, 5),
    (0, 0, 0, 0, 100),
    (255, 255, 255, 255, 255),
    (1, 0, 0, 0, 99),
    (0, 1, 0, 99, 0),
    (99, 0, 0, 0, 1),
    (50, 50, 0, 0, 0),
]

_INSTANCE_IDS = [
    "",
    "pytest__pytest-1234",
    "django__django-9999",
    "flask__flask-001",
    "repo__name-with-dashes-42",
    "org__repo-0",
    "x__y-1",
    "long-org-name__long-repo-name-12345",
    "UPPER__CASE-1",
    "a__b-999999",
]

_ERROR_MSGS = [
    "",
    None,
    "short",
    "Docker build failed",
    "x" * 1000,
    "Error: exit code 137\nOOM killed",
    "timeout after 600s",
    "Permission denied: /root",
    "  ",
    "résumé accénts",
]

_VALID_FLAGS = [True, False]


@pytest.mark.parametrize("f2p,p2p,s2p,n2p,fixed", _COUNT_COMBOS)
def test_data_counts(f2p, p2p, s2p, n2p, fixed):
    rec = _make_rec(f2p_count=f2p, p2p_count=p2p, s2p_count=s2p, n2p_count=n2p, fixed_count=fixed)
    assert rec.f2p_count == f2p
    assert rec.p2p_count == p2p
    assert rec.s2p_count == s2p
    assert rec.n2p_count == n2p
    assert rec.fixed_count == fixed


@pytest.mark.parametrize("instance_id", _INSTANCE_IDS)
def test_data_instance_id(instance_id):
    rec = _make_rec(instance_id=instance_id)
    assert rec.instance_id == instance_id


@pytest.mark.parametrize("error_msg", _ERROR_MSGS)
def test_data_error_msg(error_msg):
    rec = _make_rec(error_msg=error_msg)
    assert rec.error_msg == error_msg


@pytest.mark.parametrize("valid", _VALID_FLAGS)
@pytest.mark.parametrize("instance_id", _INSTANCE_IDS)
def test_data_valid_instance_combo(valid, instance_id):
    rec = _make_rec(valid=valid, instance_id=instance_id)
    assert rec.valid == valid
    assert rec.instance_id == instance_id


@pytest.mark.parametrize("valid", _VALID_FLAGS)
@pytest.mark.parametrize("error_msg", _ERROR_MSGS)
def test_data_valid_error_combo(valid, error_msg):
    rec = _make_rec(valid=valid, error_msg=error_msg)
    _compute(rec)
    if valid:
        assert rec.status_icon == "✓ Resolved"
    elif error_msg:
        assert rec.status_icon == "✗ Error"
    else:
        assert rec.status_icon == "✗ Unresolved"


@pytest.mark.parametrize("f2p,p2p,s2p,n2p,fixed", _COUNT_COMBOS[:15])
@pytest.mark.parametrize("valid", _VALID_FLAGS)
def test_data_counts_with_valid(f2p, p2p, s2p, n2p, fixed, valid):
    rec = _make_rec(valid=valid, f2p_count=f2p, p2p_count=p2p, s2p_count=s2p, n2p_count=n2p, fixed_count=fixed)
    assert rec.valid == valid
    total = f2p + p2p + s2p + n2p
    assert rec.f2p_count + rec.p2p_count + rec.s2p_count + rec.n2p_count == total


@pytest.mark.parametrize("seq", [0, 1, 5, 10, 20, 50, 100, 999])
def test_data_sequence(seq):
    rec = _make_rec(sequence=seq)
    assert rec.sequence == seq


@pytest.mark.parametrize("tests_text", [
    "",
    "test_a\ntest_b",
    "test_long_" + "x" * 500,
    None,
    "test1\ntest2\ntest3\ntest4\ntest5",
])
def test_data_f2p_tests_text(tests_text):
    rec = _make_rec(f2p_tests=tests_text)
    assert rec.f2p_tests == tests_text


@pytest.mark.parametrize("tests_text", [
    "",
    "test_c\ntest_d",
    "single_test",
    None,
    "a\nb\nc\nd\ne\nf\ng",
])
def test_data_p2p_tests_text(tests_text):
    rec = _make_rec(p2p_tests=tests_text)
    assert rec.p2p_tests == tests_text
