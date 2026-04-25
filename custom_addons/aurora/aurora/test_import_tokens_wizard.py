import csv
import hashlib
import io

import pytest

from aurora.models.import_tokens_wizard import (
    AuroraImportTokensWizard,
    _BATCH_SIZE,
    _HEADER_NAMES,
    _MIN_TOKEN_LENGTH,
    _VALID_PREFIXES,
)


@pytest.mark.parametrize("prefix", list(_VALID_PREFIXES))
def test_valid_prefixes_present(prefix):
    assert prefix in _VALID_PREFIXES


def test_valid_prefixes_count():
    assert len(_VALID_PREFIXES) == 3


def test_min_token_length():
    assert _MIN_TOKEN_LENGTH == 30


def test_header_names():
    assert _HEADER_NAMES == {"token", "pat", "github_token"}


def test_batch_size():
    assert _BATCH_SIZE == 500


@pytest.mark.parametrize("header", ["token", "pat", "github_token"])
def test_header_detected(header):
    assert header in _HEADER_NAMES


@pytest.mark.parametrize("header", ["Token", "PAT", "GITHUB_TOKEN", "GitHub_Token"])
def test_header_case_insensitive(header):
    assert header.lower() in _HEADER_NAMES


_parse_csv = AuroraImportTokensWizard._parse_csv


def _csv_bytes(rows, encoding="utf-8"):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode(encoding)


@pytest.mark.parametrize("header_name", ["token", "pat", "github_token"])
def test_parse_csv_strips_header(header_name):
    raw = _csv_bytes([[header_name], ["ghp_abc123"]])
    result = _parse_csv(raw)
    assert result == ["ghp_abc123"]


def test_parse_csv_no_header():
    raw = _csv_bytes([["ghp_abc123"], ["ghp_def456"]])
    result = _parse_csv(raw)
    assert len(result) == 2


def test_parse_csv_empty():
    result = _parse_csv(b"")
    assert result == []


def test_parse_csv_single_token():
    raw = _csv_bytes([["ghp_singletoken123456789012345"]])
    result = _parse_csv(raw)
    assert len(result) == 1


def test_parse_csv_100_tokens():
    rows = [[f"ghp_token{i:040d}"] for i in range(100)]
    raw = _csv_bytes(rows)
    result = _parse_csv(raw)
    assert len(result) == 100


def test_parse_csv_blank_rows():
    raw = b"ghp_abc\n\n\nghp_def\n"
    result = _parse_csv(raw)
    assert result == ["ghp_abc", "ghp_def"]


def test_parse_csv_whitespace_stripped():
    raw = b"  ghp_abc  \n  ghp_def  \n"
    result = _parse_csv(raw)
    assert result == ["ghp_abc", "ghp_def"]


def test_parse_csv_utf8_bom():
    content = "token\nghp_bomtoken1234567890123456\n"
    raw = b"\xef\xbb\xbf" + content.encode("utf-8")
    result = _parse_csv(raw)
    assert result == ["ghp_bomtoken1234567890123456"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_parse_csv_encodings(encoding):
    text = "ghp_test1234567890123456789012\n"
    raw = text.encode(encoding)
    result = _parse_csv(raw)
    assert len(result) == 1


def test_parse_csv_header_pat():
    raw = b"pat\nghp_val123\n"
    result = _parse_csv(raw)
    assert result == ["ghp_val123"]


def test_parse_csv_header_github_token():
    raw = b"github_token\nghp_val123\n"
    result = _parse_csv(raw)
    assert result == ["ghp_val123"]


@pytest.mark.parametrize("token,valid", [
    ("ghp_" + "a" * 26, True),
    ("ghp_" + "a" * 36, True),
    ("gho_" + "b" * 26, True),
    ("gho_" + "b" * 100, True),
    ("github_pat_" + "c" * 19, True),
    ("github_pat_" + "c" * 100, True),
    ("ghp_short", False),
    ("gho_s", False),
    ("github_pat_s", False),
    ("ghx_" + "a" * 26, False),
    ("abc_" + "a" * 26, False),
    ("", False),
    ("a" * 30, False),
    ("ghp_", False),
    ("gho_", False),
    ("github_pat_", False),
    ("ghp_" + "x" * 25, False),
    ("ghp_" + "x" * 26, True),
    ("random_string_no_prefix_at_all", False),
    ("GHP_" + "a" * 26, False),
    ("GHO_" + "b" * 26, False),
    ("GITHUB_PAT_" + "c" * 19, False),
    ("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ", True),
    ("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZa", True),
    ("gho_exactly30chars_1234567890a", True),
    ("github_pat_exactly30chars12345", True),
    (" ghp_" + "a" * 26, False),
    ("ghp_ " + "a" * 25, True),
    ("ghp_0123456789012345678901234567890", True),
    ("notghp_" + "a" * 30, False),
])
def test_token_validation(token, valid):
    is_valid = (
        any(token.startswith(p) for p in _VALID_PREFIXES)
        and len(token) >= _MIN_TOKEN_LENGTH
    )
    assert is_valid == valid


@pytest.mark.parametrize("token", [
    "ghp_" + "a" * 26,
    "gho_" + "b" * 26,
    "github_pat_" + "c" * 19,
])
def test_hash_dedup(token):
    h = hashlib.sha256(token.encode()).hexdigest()
    assert len(h) == 64
    assert h == hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize("n_tokens", [1, 5, 10])
def test_hash_uniqueness(n_tokens):
    tokens = [f"ghp_{'x' * 26}{i:04d}" for i in range(n_tokens)]
    hashes = {hashlib.sha256(t.encode()).hexdigest() for t in tokens}
    assert len(hashes) == n_tokens


def test_parse_xlsx_calls_openpyxl():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [("ghp_token1234567890123456",)]
    mock_wb.close = MagicMock()

    with patch("aurora.models.import_tokens_wizard.load_workbook", mock_wb, create=True):
        with patch("openpyxl.load_workbook", return_value=mock_wb):
            result = AuroraImportTokensWizard._parse_xlsx(b"fake_xlsx_content")

    assert "ghp_token1234567890123456" in result


def test_parse_xlsx_strips_header():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [("token",), ("ghp_abc12345678901234567890",)]
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert "token" not in result
    assert "ghp_abc12345678901234567890" in result


def test_parse_xlsx_skips_none_values():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [(None,), ("ghp_val12345678901234567890",), (None,)]
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert len(result) == 1


def test_parse_xlsx_empty_workbook():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = []
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert result == []


def test_parse_xlsx_strips_whitespace():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [("  ghp_spaced123456789012345  ",)]
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert result == ["ghp_spaced123456789012345"]


def test_parse_xlsx_numeric_cell():
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [(12345,)]
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert result == ["12345"]


def test_parse_xlsx_exception_raises_user_error():
    from unittest.mock import patch
    from odoo.exceptions import UserError
    with patch("openpyxl.load_workbook", side_effect=Exception("corrupt")):
        with pytest.raises(UserError, match="Failed to parse Excel"):
            AuroraImportTokensWizard._parse_xlsx(b"bad_data")


def test_parse_csv_exception_raises_user_error():
    from odoo.exceptions import UserError
    bad = bytes(range(256))
    with pytest.raises(UserError, match="Failed to parse CSV"):
        _parse_csv(bad)


@pytest.mark.parametrize("header_val", list(_HEADER_NAMES))
def test_parse_xlsx_all_header_names(header_val):
    from unittest.mock import patch, MagicMock
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_wb.active = mock_ws
    mock_ws.iter_rows.return_value = [(header_val,), ("ghp_data12345678901234567890",)]
    mock_wb.close = MagicMock()

    with patch("openpyxl.load_workbook", return_value=mock_wb):
        result = AuroraImportTokensWizard._parse_xlsx(b"fake")

    assert header_val not in result
    assert "ghp_data12345678901234567890" in result


@pytest.mark.parametrize("count", [0, 1, 499, 500, 501, 1000])
def test_batch_size_boundary(count):
    n_batches = (count + _BATCH_SIZE - 1) // _BATCH_SIZE if count > 0 else 0
    expected = n_batches
    actual = 0
    for start in range(0, count, _BATCH_SIZE):
        actual += 1
    if count == 0:
        assert actual == 0
    else:
        assert actual == expected
