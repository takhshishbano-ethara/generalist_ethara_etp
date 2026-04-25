import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aurora.tools.phase2_docker_build import _translate_phase1_jsonl


@pytest.fixture(autouse=True)
def _patch_ranges(monkeypatch, tmp_dir):
    repos_root = tmp_dir / "repos"
    monkeypatch.setattr(
        "aurora.tools.phase2_docker_build._HARNESS_REPOS_ROOT", repos_root
    )
    for lang in ("python", "javascript", "java", "rust", "go", "typescript"):
        (repos_root / lang).mkdir(parents=True, exist_ok=True)


def _write_input(tmp_dir, lines, fname="input.jsonl"):
    p = tmp_dir / fname
    with open(p, "w") as f:
        for line in lines:
            f.write(line + "\n")
    return str(p)


def _read_output(path):
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _setup_ranges(tmp_dir, org, lang, files):
    d = tmp_dir / "repos" / lang / org
    d.mkdir(parents=True, exist_ok=True)
    for fn in files:
        (d / fn).touch()


_INT_NUMBERS = [
    (1, 1),
    (0, 0),
    (100, 100),
    (9999, 9999),
    (42, 42),
    (500, 500),
    (2813, 2813),
    (3055, 3055),
    (1000000, 1000000),
    (2, 2),
]


class TestTranslateIntegerNumbers:

    @pytest.mark.parametrize("number,expected", _INT_NUMBERS)
    def test_integer_passthrough(self, tmp_dir, number, expected):
        inp = _write_input(tmp_dir, [json.dumps({"number": number})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 1
        records = _read_output(out)
        assert records[0]["number"] == expected


_STR_HYPHEN_NUMBERS = [
    ("2500-2501", 2500),
    ("100-200", 100),
    ("1-2", 1),
    ("999-1000", 999),
    ("0-1", 0),
    ("50-60-70", 50),
    ("2813-2814", 2813),
    ("3055-3056", 3055),
    ("10-20-30", 10),
    ("500-600", 500),
    ("42-43", 42),
    ("7777-8888", 7777),
]


class TestTranslateHyphenNumbers:

    @pytest.mark.parametrize("raw,expected", _STR_HYPHEN_NUMBERS)
    def test_hyphen_first_part(self, tmp_dir, raw, expected):
        inp = _write_input(tmp_dir, [json.dumps({"number": raw})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 1
        records = _read_output(out)
        assert records[0]["number"] == expected


_STR_PLAIN_NUMBERS = [
    ("100", 100),
    ("0", 0),
    ("9999", 9999),
    ("1", 1),
    ("42", 42),
    ("2813", 2813),
    ("500", 500),
]


class TestTranslateStringNumbers:

    @pytest.mark.parametrize("raw,expected", _STR_PLAIN_NUMBERS)
    def test_string_int(self, tmp_dir, raw, expected):
        inp = _write_input(tmp_dir, [json.dumps({"number": raw})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 1
        records = _read_output(out)
        assert records[0]["number"] == expected


_INVALID_NUMBERS = [
    "abc",
    "",
    "hello",
    "abc-def",
    "-",
    "x-y-z",
    "nan",
    "inf",
    "None",
    "True",
    "false",
    "null",
]


class TestTranslateInvalidNumbers:

    @pytest.mark.parametrize("raw", _INVALID_NUMBERS)
    def test_invalid_skipped(self, tmp_dir, raw):
        inp = _write_input(tmp_dir, [json.dumps({"number": raw})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 0


class TestTranslateEmptyInput:

    def test_empty_file(self, tmp_dir):
        inp = _write_input(tmp_dir, [])
        out = str(tmp_dir / "out.jsonl")
        assert _translate_phase1_jsonl(inp, "encode", "starlette", "python", out) == 0

    def test_only_blank_lines(self, tmp_dir):
        inp = _write_input(tmp_dir, ["", "  ", "   "])
        out = str(tmp_dir / "out.jsonl")
        assert _translate_phase1_jsonl(inp, "encode", "starlette", "python", out) == 0


class TestTranslateInvalidJson:

    @pytest.mark.parametrize("bad_line", [
        "not json",
        "{bad}",
        "{'single': 'quotes'}",
        "[1,2,3",
        "just text here",
        "{\"key\": }",
    ])
    def test_invalid_json_skipped(self, tmp_dir, bad_line):
        inp = _write_input(tmp_dir, [bad_line])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 0


class TestTranslateBlankLines:

    @pytest.mark.parametrize("lines", [
        ["", json.dumps({"number": 100})],
        [json.dumps({"number": 100}), ""],
        ["", json.dumps({"number": 100}), ""],
        ["   ", json.dumps({"number": 100}), "  "],
        ["", "", json.dumps({"number": 100}), "", ""],
    ])
    def test_blanks_skipped(self, tmp_dir, lines):
        inp = _write_input(tmp_dir, lines)
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 1


class TestTranslateMixedRecords:

    @pytest.mark.parametrize("valid_count,invalid_count", [
        (1, 1),
        (2, 1),
        (3, 0),
        (0, 3),
        (5, 5),
        (1, 0),
        (10, 2),
        (0, 1),
    ])
    def test_mixed_valid_invalid(self, tmp_dir, valid_count, invalid_count):
        lines = []
        for i in range(valid_count):
            lines.append(json.dumps({"number": 100 + i}))
        for _ in range(invalid_count):
            lines.append(json.dumps({"number": "abc"}))
        inp = _write_input(tmp_dir, lines)
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == valid_count


class TestTranslateWithRanges:

    def _setup(self, tmp_dir, files):
        _setup_ranges(tmp_dir, "encode", "python", files)

    @pytest.mark.parametrize("number,range_files,expected_count", [
        (2900, ["starlette_3055_to_2813.py"], 1),
        (2813, ["starlette_3055_to_2813.py"], 1),
        (3055, ["starlette_3055_to_2813.py"], 1),
        (100, ["starlette_3055_to_2813.py"], 0),
        (3056, ["starlette_3055_to_2813.py"], 0),
        (2812, ["starlette_3055_to_2813.py"], 0),
        (0, ["starlette_3055_to_2813.py"], 0),
        (75, ["starlette_100_to_50.py"], 1),
        (50, ["starlette_100_to_50.py"], 1),
        (100, ["starlette_100_to_50.py"], 1),
        (49, ["starlette_100_to_50.py"], 0),
        (101, ["starlette_100_to_50.py"], 0),
    ])
    def test_range_filtering(self, tmp_dir, number, range_files, expected_count):
        self._setup(tmp_dir, range_files)
        inp = _write_input(tmp_dir, [json.dumps({"number": number})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == expected_count

    def test_range_sets_number_interval(self, tmp_dir):
        self._setup(tmp_dir, ["starlette_3055_to_2813.py"])
        inp = _write_input(tmp_dir, [json.dumps({"number": 2900})])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        assert records[0]["number_interval"] == "starlette_3055_to_2813"

    def test_multi_ranges(self, tmp_dir):
        self._setup(tmp_dir, ["starlette_100_to_50.py", "starlette_300_to_200.py"])
        inp = _write_input(tmp_dir, [
            json.dumps({"number": 75}),
            json.dumps({"number": 250}),
            json.dumps({"number": 150}),
        ])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 2
        records = _read_output(out)
        assert records[0]["number_interval"] == "starlette_100_to_50"
        assert records[1]["number_interval"] == "starlette_300_to_200"


class TestTranslateNoRangesDefaults:

    def test_number_interval_defaults_empty(self, tmp_dir):
        inp = _write_input(tmp_dir, [json.dumps({"number": 100})])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        assert records[0]["number_interval"] == ""

    def test_existing_number_interval_preserved(self, tmp_dir):
        inp = _write_input(tmp_dir, [json.dumps({"number": 100, "number_interval": "custom"})])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        assert records[0]["number_interval"] == "custom"


class TestTranslateDefaultFields:

    @pytest.mark.parametrize("has_tag", [True, False])
    def test_tag_default(self, tmp_dir, has_tag):
        rec = {"number": 100}
        if has_tag:
            rec["tag"] = "v1.0"
        inp = _write_input(tmp_dir, [json.dumps(rec)])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        if has_tag:
            assert records[0]["tag"] == "v1.0"
        else:
            assert records[0]["tag"] == ""

    @pytest.mark.parametrize("has_lang", [True, False])
    def test_lang_default(self, tmp_dir, has_lang):
        rec = {"number": 100}
        if has_lang:
            rec["lang"] = "javascript"
        inp = _write_input(tmp_dir, [json.dumps(rec)])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        if has_lang:
            assert records[0]["lang"] == "javascript"
        else:
            assert records[0]["lang"] == "python"

    @pytest.mark.parametrize("lang", ["python", "javascript", "java", "rust", "go", "typescript"])
    def test_lang_param_used(self, tmp_dir, lang):
        inp = _write_input(tmp_dir, [json.dumps({"number": 100})])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", lang, out)
        records = _read_output(out)
        assert records[0]["lang"] == lang


class TestTranslateOutputFormat:

    def test_one_json_per_line(self, tmp_dir):
        recs = [{"number": i} for i in range(5)]
        inp = _write_input(tmp_dir, [json.dumps(r) for r in recs])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        with open(out) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert len(lines) == 5
        for line in lines:
            json.loads(line)

    def test_return_count_matches_written(self, tmp_dir):
        recs = [{"number": i} for i in range(7)]
        inp = _write_input(tmp_dir, [json.dumps(r) for r in recs])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        assert count == len(records) == 7


class TestTranslateLargeDataset:

    @pytest.mark.parametrize("n", [50, 100])
    def test_large_batch(self, tmp_dir, n):
        recs = [json.dumps({"number": i}) for i in range(n)]
        inp = _write_input(tmp_dir, recs)
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == n


_ORG_REPO_LANG_COMBOS = [
    ("encode", "starlette", "python"),
    ("pallets", "flask", "python"),
    ("django", "django", "python"),
    ("encode", "httpx", "python"),
    ("psf", "requests", "python"),
    ("numpy", "numpy", "python"),
    ("encode", "http-x", "python"),
    ("scipy", "scipy", "python"),
]


class TestTranslateOrgRepoLang:

    @pytest.mark.parametrize("org,repo,lang", _ORG_REPO_LANG_COMBOS)
    def test_various_combos(self, tmp_dir, org, repo, lang):
        inp = _write_input(tmp_dir, [json.dumps({"number": 100})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, org, repo, lang, out)
        assert count == 1

    @pytest.mark.parametrize("org,repo,lang", _ORG_REPO_LANG_COMBOS)
    def test_with_range_files(self, tmp_dir, org, repo, lang):
        repo_lower = repo.lower().replace("-", "_")
        _setup_ranges(tmp_dir, org, lang, [f"{repo_lower}_500_to_100.py"])
        inp = _write_input(tmp_dir, [json.dumps({"number": 300})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, org, repo, lang, out)
        assert count == 1
        records = _read_output(out)
        assert records[0]["number_interval"] != ""


class TestTranslateExtraFields:

    @pytest.mark.parametrize("extra", [
        {"title": "Fix bug"},
        {"body": "Long description"},
        {"title": "Fix", "body": "desc", "labels": ["bug"]},
        {"custom_key": "custom_value"},
        {},
    ])
    def test_extra_fields_preserved(self, tmp_dir, extra):
        rec = {"number": 100, **extra}
        inp = _write_input(tmp_dir, [json.dumps(rec)])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        for k, v in extra.items():
            assert records[0][k] == v


class TestTranslateMissingNumber:

    def test_missing_number_key(self, tmp_dir):
        inp = _write_input(tmp_dir, [json.dumps({"title": "no number"})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 0

    def test_number_none(self, tmp_dir):
        inp = _write_input(tmp_dir, [json.dumps({"number": None})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == 0


_RANGE_BOUNDARY_CASES = [
    (50, ["starlette_100_to_50.py"], 1),
    (100, ["starlette_100_to_50.py"], 1),
    (51, ["starlette_100_to_50.py"], 1),
    (99, ["starlette_100_to_50.py"], 1),
    (49, ["starlette_100_to_50.py"], 0),
    (101, ["starlette_100_to_50.py"], 0),
    (0, ["starlette_100_to_50.py"], 0),
    (200, ["starlette_100_to_50.py"], 0),
    (1000, ["starlette_100_to_50.py"], 0),
    (75, ["starlette_100_to_50.py"], 1),
    (60, ["starlette_100_to_50.py"], 1),
    (90, ["starlette_100_to_50.py"], 1),
    (25, ["starlette_100_to_50.py"], 0),
]


class TestTranslateRangeBoundary:

    @pytest.mark.parametrize("number,range_files,expected_count", _RANGE_BOUNDARY_CASES)
    def test_boundary(self, tmp_dir, number, range_files, expected_count):
        _setup_ranges(tmp_dir, "encode", "python", range_files)
        inp = _write_input(tmp_dir, [json.dumps({"number": number})])
        out = str(tmp_dir / "out.jsonl")
        count = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert count == expected_count


_MULTI_RANGE_INTERVAL_NAMES = [
    (75, ["starlette_100_to_50.py", "starlette_300_to_200.py"], "starlette_100_to_50"),
    (250, ["starlette_100_to_50.py", "starlette_300_to_200.py"], "starlette_300_to_200"),
    (2900, ["starlette_3055_to_2813.py", "starlette_100_to_50.py"], "starlette_3055_to_2813"),
    (60, ["starlette_3055_to_2813.py", "starlette_100_to_50.py"], "starlette_100_to_50"),
]


class TestTranslateMultiRangeIntervalName:

    @pytest.mark.parametrize("number,range_files,expected_name", _MULTI_RANGE_INTERVAL_NAMES)
    def test_correct_interval_name(self, tmp_dir, number, range_files, expected_name):
        _setup_ranges(tmp_dir, "encode", "python", range_files)
        inp = _write_input(tmp_dir, [json.dumps({"number": number})])
        out = str(tmp_dir / "out.jsonl")
        _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        records = _read_output(out)
        assert records[0]["number_interval"] == expected_name


_SEQUENCE_COUNTS = [1, 2, 3, 4, 5, 7, 10, 15, 20, 25, 30, 40, 50]


class TestTranslateSequentialNumbers:

    @pytest.mark.parametrize("count", _SEQUENCE_COUNTS)
    def test_sequential(self, tmp_dir, count):
        lines = [json.dumps({"number": i + 1}) for i in range(count)]
        inp = _write_input(tmp_dir, lines)
        out = str(tmp_dir / "out.jsonl")
        result = _translate_phase1_jsonl(inp, "encode", "starlette", "python", out)
        assert result == count
        records = _read_output(out)
        assert len(records) == count
        for i, r in enumerate(records):
            assert r["number"] == i + 1
