import json
import pytest

from aurora.models.preview_wizard import (
    AuroraPipelinePreview,
    _MAX_CHARS_PER_LINE,
    _MAX_PREVIEW_LINES,
)

_build = AuroraPipelinePreview._build_preview


# ---------------------------------------------------------------------------
# Basic record counts — parametrize line counts × total_count
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_lines", [1, 5, 10, 50, 51])
@pytest.mark.parametrize("total_count", [0, 50, 100, 1000])
def test_record_count_combinations(sample_jsonl_file, n_lines, total_count):
    records = [{"i": i} for i in range(n_lines)]
    path = sample_jsonl_file(records)
    text, shown = _build(path, total_count)
    expected_shown = min(n_lines, _MAX_PREVIEW_LINES)
    assert shown == expected_shown
    assert text


# ---------------------------------------------------------------------------
# Empty file
# ---------------------------------------------------------------------------
def test_empty_file_raises(empty_file):
    path = empty_file()
    with pytest.raises(UnboundLocalError):
        _build(path, 0)


def test_empty_file_with_total_count_raises(empty_file):
    path = empty_file()
    with pytest.raises(UnboundLocalError):
        _build(path, 100)


# ---------------------------------------------------------------------------
# File with only blank lines
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_blanks", [1, 3, 10])
def test_blank_lines_only(tmp_dir, n_blanks):
    p = tmp_dir / "blanks.jsonl"
    p.write_text("\n" * n_blanks)
    text, shown = _build(str(p), 0)
    assert text == ""


# ---------------------------------------------------------------------------
# Separator "---" between records
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n_records", [2, 3, 5])
def test_separator_between_records(sample_jsonl_file, n_records):
    records = [{"k": i} for i in range(n_records)]
    path = sample_jsonl_file(records)
    text, _ = _build(path, n_records)
    assert text.count("\n---\n") == n_records - 1


def test_single_record_no_separator(sample_jsonl_file):
    path = sample_jsonl_file([{"a": 1}])
    text, _ = _build(path, 1)
    assert "---" not in text


# ---------------------------------------------------------------------------
# "… and N more records" message
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("total_count,expected_more", [
    (100, 50),
    (1000, 950),
    (51, 1),
])
def test_more_records_message(sample_jsonl_file, total_count, expected_more):
    records = [{"x": i} for i in range(55)]
    path = sample_jsonl_file(records)
    text, shown = _build(path, total_count)
    assert f"… and {expected_more} more records" in text
    assert shown == _MAX_PREVIEW_LINES


def test_no_more_message_when_exact_50(sample_jsonl_file):
    records = [{"x": i} for i in range(50)]
    path = sample_jsonl_file(records)
    text, _ = _build(path, 50)
    assert "… and" not in text


def test_more_message_with_zero_total(sample_jsonl_file):
    records = [{"x": i} for i in range(55)]
    path = sample_jsonl_file(records)
    text, _ = _build(path, 0)
    assert "… and" not in text


# ---------------------------------------------------------------------------
# JSON valid lines — pretty printing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("obj", [
    {"key": "value"},
    {"nested": {"a": [1, 2, 3]}},
    [1, 2, 3],
    {"num": 42, "float": 3.14, "bool": True, "null": None},
    {},
    [],
])
def test_valid_json_pretty_printed(sample_jsonl_file, obj):
    path = sample_jsonl_file([obj])
    text, shown = _build(path, 1)
    assert shown == 1
    reparsed = json.loads(text)
    assert reparsed == obj


# ---------------------------------------------------------------------------
# Invalid JSON lines
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_line", [
    "not json at all",
    "{invalid",
    "True",
    "",
    "   ",
])
def test_invalid_json_lines(tmp_dir, bad_line):
    p = tmp_dir / "bad.jsonl"
    content = bad_line + "\n" + json.dumps({"ok": 1}) + "\n"
    p.write_text(content)
    text, shown = _build(str(p), 2)
    if bad_line.strip():
        assert bad_line.strip()[:_MAX_CHARS_PER_LINE] in text


# ---------------------------------------------------------------------------
# Lines exceeding _MAX_CHARS_PER_LINE
# ---------------------------------------------------------------------------
def test_long_json_truncated(sample_jsonl_file):
    big = {"data": "x" * 1000}
    path = sample_jsonl_file([big])
    text, _ = _build(path, 1)
    assert "…(truncated)" in text
    lines = text.split("\n")
    non_trunc = [l for l in lines if "…(truncated)" not in l]
    for l in non_trunc:
        assert len(l) <= _MAX_CHARS_PER_LINE + 50


def test_long_invalid_line_truncated(tmp_dir):
    long_line = "A" * 1000
    p = tmp_dir / "long.jsonl"
    p.write_text(long_line + "\n")
    text, _ = _build(str(p), 1)
    assert len(text) <= _MAX_CHARS_PER_LINE


@pytest.mark.parametrize("length", [599, 600, 601, 1200])
def test_truncation_boundary(tmp_dir, length):
    obj = {"d": "z" * length}
    p = tmp_dir / "boundary.jsonl"
    p.write_text(json.dumps(obj) + "\n")
    text, _ = _build(str(p), 1)
    pretty = json.dumps(obj, indent=2, ensure_ascii=False)
    if len(pretty) > _MAX_CHARS_PER_LINE:
        assert "…(truncated)" in text
    else:
        assert "…(truncated)" not in text


# ---------------------------------------------------------------------------
# Unicode content
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("content", [
    {"emoji": "🎉🚀"},
    {"chinese": "你好世界"},
    {"arabic": "مرحبا"},
    {"mixed": "café résumé naïve"},
])
def test_unicode_content(sample_jsonl_file, content):
    path = sample_jsonl_file([content])
    text, shown = _build(path, 1)
    assert shown == 1
    reparsed = json.loads(text)
    assert reparsed == content


# ---------------------------------------------------------------------------
# Nested JSON objects
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("depth", [2, 5, 10])
def test_nested_objects(sample_jsonl_file, depth):
    obj = {"level": 0}
    current = obj
    for i in range(1, depth):
        current["child"] = {"level": i}
        current = current["child"]
    path = sample_jsonl_file([obj])
    text, shown = _build(path, 1)
    assert shown == 1
    assert "level" in text


# ---------------------------------------------------------------------------
# Mixed valid / invalid / blank lines
# ---------------------------------------------------------------------------
def test_mixed_content(tmp_dir):
    lines = [
        json.dumps({"a": 1}),
        "",
        "bad line",
        json.dumps({"b": 2}),
        "   ",
        json.dumps({"c": 3}),
    ]
    p = tmp_dir / "mixed.jsonl"
    p.write_text("\n".join(lines) + "\n")
    text, shown = _build(str(p), 6)
    assert "---" in text
    assert '"a": 1' in text or '"a":1' in text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def test_max_preview_lines_value():
    assert _MAX_PREVIEW_LINES == 50


def test_max_chars_per_line_value():
    assert _MAX_CHARS_PER_LINE == 600


def test_max_preview_lines_is_int():
    assert isinstance(_MAX_PREVIEW_LINES, int)


def test_max_chars_per_line_is_int():
    assert isinstance(_MAX_CHARS_PER_LINE, int)


# ---------------------------------------------------------------------------
# Exactly at boundary: 50 lines with total_count > 50 → no "more" message
# ---------------------------------------------------------------------------
def test_exactly_50_lines_total_100(sample_jsonl_file):
    records = [{"i": i} for i in range(50)]
    path = sample_jsonl_file(records)
    text, shown = _build(path, 100)
    assert shown == 50
    assert "… and" not in text


# ---------------------------------------------------------------------------
# 51 lines: first 50 shown + "more" msg
# ---------------------------------------------------------------------------
def test_51_lines_shows_more(sample_jsonl_file):
    records = [{"i": i} for i in range(51)]
    path = sample_jsonl_file(records)
    text, shown = _build(path, 51)
    assert shown == 50
    assert "… and 1 more records" in text
