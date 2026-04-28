import os
import sys
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from aurora.models.s3_storage import (
    _build_base_prefix,
    _RUN_PREFIX_RE,
    _S3_MAX_UPLOAD_ATTEMPTS,
    _S3_RETRY_BACKOFF_BASE,
    build_s3_key,
    generate_presigned_url,
    get_next_run_number,
    is_configured,
    upload_file,
    validate_credentials,
)


# ---------------------------------------------------------------------------
# is_configured — 32 parametrized combos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bucket, access_key, secret_key, expected",
    [
        ("b", "a", "s", True),
        ("my-bucket", "AKIA123", "secret", True),
        ("", "a", "s", False),
        ("b", "", "s", False),
        ("b", "a", "", False),
        ("", "", "", False),
        ("", "", "s", False),
        ("", "a", "", False),
        ("b", "", "", False),
        (None, "a", "s", False),
        ("b", None, "s", False),
        ("b", "a", None, False),
        (None, None, None, False),
        (None, None, "s", False),
        (None, "a", None, False),
        ("b", None, None, False),
        (" ", "a", "s", True),
        ("b", " ", "s", True),
        ("b", "a", " ", True),
        (" ", " ", " ", True),
        ("bucket-name", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", True),
        ("0", "0", "0", True),
        (0, "a", "s", False),
        ("b", 0, "s", False),
        ("b", "a", 0, False),
        (False, "a", "s", False),
        ("b", False, "s", False),
        ("b", "a", False, False),
        ("x" * 1000, "a", "s", True),
        ("b", "a" * 500, "s", True),
        ("b", "a", "s" * 500, True),
        ("bucket.with.dots", "key/with/slashes", "sec+ret=chars", True),
    ],
    ids=[
        "all-valid", "realistic-valid", "empty-bucket", "empty-key", "empty-secret",
        "all-empty", "only-secret", "only-key", "only-bucket",
        "none-bucket", "none-key", "none-secret", "all-none",
        "none-bucket-key", "none-bucket-secret", "none-key-secret",
        "space-bucket", "space-key", "space-secret", "all-spaces",
        "long-realistic", "zero-strings", "int-bucket", "int-key", "int-secret",
        "false-bucket", "false-key", "false-secret",
        "very-long-bucket", "very-long-key", "very-long-secret",
        "special-chars",
    ],
)
def test_is_configured(bucket, access_key, secret_key, expected):
    cfg = {"bucket": bucket, "access_key": access_key, "secret_key": secret_key}
    assert is_configured(cfg) is expected


# ---------------------------------------------------------------------------
# _build_base_prefix — 22 parametrized combos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "org, repo, folder, expected",
    [
        ("myorg", "myrepo", "", "aurora_phase1/myorg__myrepo/"),
        ("myorg", "myrepo", "f", "f/aurora_phase1/myorg__myrepo/"),
        ("org", "repo", "folder", "folder/aurora_phase1/org__repo/"),
        ("org", "repo", "/folder/", "folder/aurora_phase1/org__repo/"),
        ("org", "repo", "folder/", "folder/aurora_phase1/org__repo/"),
        ("org", "repo", "/folder", "folder/aurora_phase1/org__repo/"),
        ("org", "repo", "a/b/c", "a/b/c/aurora_phase1/org__repo/"),
        ("org", "repo", "/a/b/c/", "a/b/c/aurora_phase1/org__repo/"),
        ("A", "B", "", "aurora_phase1/A__B/"),
        ("a-b", "c-d", "", "aurora_phase1/a-b__c-d/"),
        ("a_b", "c_d", "", "aurora_phase1/a_b__c_d/"),
        ("org", "repo", "  ", "  /aurora_phase1/org__repo/"),
        ("org", "repo", " / ", " / /aurora_phase1/org__repo/"),
        ("ORG", "REPO", "FOLDER", "FOLDER/aurora_phase1/ORG__REPO/"),
        ("123", "456", "789", "789/aurora_phase1/123__456/"),
        ("o", "r", "deep/nested/path", "deep/nested/path/aurora_phase1/o__r/"),
        ("org.x", "repo.y", "", "aurora_phase1/org.x__repo.y/"),
        ("org", "repo", None, "aurora_phase1/org__repo/"),
        ("org", "repo", "///", "aurora_phase1/org__repo/"),
        ("org", "repo", "a///b", "a///b/aurora_phase1/org__repo/"),
        ("o-r-g", "r-e-p-o", "f-o-l-d", "f-o-l-d/aurora_phase1/o-r-g__r-e-p-o/"),
        ("", "", "", "aurora_phase1/__/"),
    ],
    ids=[
        "no-folder", "short-folder", "normal-folder", "slash-wrapped",
        "trailing-slash", "leading-slash", "nested-folder", "nested-slash-wrapped",
        "single-char", "hyphen-names", "underscore-names", "space-only-folder",
        "space-slash-folder", "uppercase", "numeric", "deep-nested",
        "dotted-names", "none-folder", "only-slashes", "double-slashes-in-folder",
        "hyphenated-all", "empty-org-repo",
    ],
)
def test_build_base_prefix(org, repo, folder, expected):
    result = _build_base_prefix(org, repo, folder)
    assert result == expected


# ---------------------------------------------------------------------------
# build_s3_key — 12 parametrized combos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "org, repo, run_number, filename, folder, expected",
    [
        ("o", "r", 1, "f.jsonl", "", "aurora_phase1/o__r/run_1/f.jsonl"),
        ("o", "r", 42, "data.csv", "fold", "fold/aurora_phase1/o__r/run_42/data.csv"),
        ("x", "y", 100, "a.txt", "", "aurora_phase1/x__y/run_100/a.txt"),
        ("x", "y", 0, "z.log", "", "aurora_phase1/x__y/run_0/z.log"),
        ("a-b", "c-d", 7, "out.json", "/p/", "p/aurora_phase1/a-b__c-d/run_7/out.json"),
        ("org", "repo", 999, "file name.txt", "", "aurora_phase1/org__repo/run_999/file name.txt"),
        ("org", "repo", 1, "日本語.csv", "", "aurora_phase1/org__repo/run_1/日本語.csv"),
        ("o", "r", 1, "a/b/c.txt", "", "aurora_phase1/o__r/run_1/a/b/c.txt"),
        ("o", "r", 10, ".hidden", "d", "d/aurora_phase1/o__r/run_10/.hidden"),
        ("o", "r", 5, "UPPER.TXT", "", "aurora_phase1/o__r/run_5/UPPER.TXT"),
        ("o", "r", 3, "file.tar.gz", "f", "f/aurora_phase1/o__r/run_3/file.tar.gz"),
        ("oo", "rr", 2, "x", "", "aurora_phase1/oo__rr/run_2/x"),
    ],
    ids=[
        "basic", "with-folder", "large-run", "zero-run", "slashed-folder",
        "space-filename", "unicode-filename", "nested-filename", "hidden-file",
        "uppercase-file", "double-ext", "minimal",
    ],
)
def test_build_s3_key(org, repo, run_number, filename, folder, expected):
    assert build_s3_key(org, repo, run_number, filename, folder) == expected


# ---------------------------------------------------------------------------
# _RUN_PREFIX_RE — 6 parametrized cases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, should_match, expected_num",
    [
        ("run_1/", True, 1),
        ("run_42/", True, 42),
        ("run_999/", True, 999),
        ("run_0/", True, 0),
        ("run_/", False, None),
        ("notrun_1/", False, None),
    ],
    ids=["run1", "run42", "run999", "run0", "no-number", "bad-prefix"],
)
def test_run_prefix_re(text, should_match, expected_num):
    m = _RUN_PREFIX_RE.match(text)
    if should_match:
        assert m is not None
        assert int(m.group(1)) == expected_num
    else:
        assert m is None


# ---------------------------------------------------------------------------
# validate_credentials — 3 tests
# ---------------------------------------------------------------------------
@patch("aurora.models.s3_storage._get_client")
def test_validate_credentials_success(mock_gc):
    client = MagicMock()
    mock_gc.return_value = client
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    validate_credentials(cfg)
    client.head_bucket.assert_called_once_with(Bucket="b")


@patch("aurora.models.s3_storage._get_client")
def test_validate_credentials_failure(mock_gc):
    client = MagicMock()
    client.head_bucket.side_effect = Exception("403 Forbidden")
    mock_gc.return_value = client
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    with pytest.raises(Exception, match="403 Forbidden"):
        validate_credentials(cfg)


@patch("aurora.models.s3_storage._get_client")
def test_validate_credentials_called_with_config(mock_gc):
    client = MagicMock()
    mock_gc.return_value = client
    cfg = {"bucket": "mybucket", "access_key": "ak", "secret_key": "sk", "region": "eu-west-1"}
    validate_credentials(cfg)
    mock_gc.assert_called_once_with(cfg)


# ---------------------------------------------------------------------------
# get_next_run_number — 8 parametrized cases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "common_prefixes, expected",
    [
        ([], 1),
        ([{"Prefix": "aurora_phase1/o__r/run_1/"}], 2),
        ([{"Prefix": "aurora_phase1/o__r/run_1/"}, {"Prefix": "aurora_phase1/o__r/run_5/"}], 6),
        ([{"Prefix": "aurora_phase1/o__r/run_10/"}, {"Prefix": "aurora_phase1/o__r/run_2/"}], 11),
        ([{"Prefix": "aurora_phase1/o__r/run_99/"}], 100),
        ([{"Prefix": "aurora_phase1/o__r/other_dir/"}], 1),
        ([{"Prefix": "aurora_phase1/o__r/run_3/"}, {"Prefix": "aurora_phase1/o__r/junk/"}], 4),
        ([{"Prefix": "aurora_phase1/o__r/run_0/"}], 1),
    ],
    ids=[
        "empty", "single-run", "multi-run", "unordered", "high-number",
        "no-matching-folders", "mixed", "run-zero",
    ],
)
@patch("aurora.models.s3_storage._get_client")
def test_get_next_run_number(mock_gc, common_prefixes, expected):
    client = MagicMock()
    mock_gc.return_value = client
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"CommonPrefixes": common_prefixes}]
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    assert get_next_run_number(cfg, "o", "r") == expected


# ---------------------------------------------------------------------------
# upload_file — retry logic with parametrized failure counts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fail_count",
    [0, 1, 2],
    ids=["no-fail", "one-retry", "two-retries"],
)
@patch("aurora.models.s3_storage.time.sleep")
@patch("aurora.models.s3_storage._get_transfer_config")
@patch("aurora.models.s3_storage._get_client")
def test_upload_file_retries(mock_gc, mock_tc, mock_sleep, fail_count, tmp_path):
    client = MagicMock()
    mock_gc.return_value = client
    mock_tc.return_value = MagicMock()
    f = tmp_path / "test.txt"
    f.write_text("data")
    calls = {"n": 0}
    def side_effect(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= fail_count:
            raise Exception("network error")
    client.upload_file.side_effect = side_effect
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s", "region": "us-east-1"}
    result = upload_file(cfg, str(f), "key/file.txt")
    assert "s3" in result
    assert client.upload_file.call_count == fail_count + 1


@patch("aurora.models.s3_storage.time.sleep")
@patch("aurora.models.s3_storage._get_transfer_config")
@patch("aurora.models.s3_storage._get_client")
def test_upload_file_exhausted_retries(mock_gc, mock_tc, mock_sleep, tmp_path):
    client = MagicMock()
    mock_gc.return_value = client
    mock_tc.return_value = MagicMock()
    f = tmp_path / "test.txt"
    f.write_text("data")
    client.upload_file.side_effect = Exception("persistent failure")
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s", "region": "us-east-1"}
    with pytest.raises(Exception, match="persistent failure"):
        upload_file(cfg, str(f), "key/file.txt")
    assert client.upload_file.call_count == _S3_MAX_UPLOAD_ATTEMPTS


@patch("aurora.models.s3_storage.time.sleep")
@patch("aurora.models.s3_storage._get_transfer_config")
@patch("aurora.models.s3_storage._get_client")
def test_upload_file_returns_url_format(mock_gc, mock_tc, mock_sleep, tmp_path):
    client = MagicMock()
    mock_gc.return_value = client
    mock_tc.return_value = MagicMock()
    f = tmp_path / "data.jsonl"
    f.write_text("content")
    cfg = {"bucket": "my-bucket", "access_key": "a", "secret_key": "s", "region": "eu-west-1"}
    url = upload_file(cfg, str(f), "prefix/data.jsonl")
    assert url == "https://my-bucket.s3.eu-west-1.amazonaws.com/prefix/data.jsonl"


@patch("aurora.models.s3_storage.time.sleep")
@patch("aurora.models.s3_storage._get_transfer_config")
@patch("aurora.models.s3_storage._get_client")
def test_upload_file_default_region(mock_gc, mock_tc, mock_sleep, tmp_path):
    client = MagicMock()
    mock_gc.return_value = client
    mock_tc.return_value = MagicMock()
    f = tmp_path / "d.txt"
    f.write_text("x")
    cfg = {"bucket": "bkt", "access_key": "a", "secret_key": "s"}
    url = upload_file(cfg, str(f), "k")
    assert "ap-south-1" in url


@patch("aurora.models.s3_storage.time.sleep")
@patch("aurora.models.s3_storage._get_transfer_config")
@patch("aurora.models.s3_storage._get_client")
def test_upload_file_sleep_called_on_retry(mock_gc, mock_tc, mock_sleep, tmp_path):
    client = MagicMock()
    mock_gc.return_value = client
    mock_tc.return_value = MagicMock()
    f = tmp_path / "d.txt"
    f.write_text("x")
    calls = {"n": 0}
    def side_effect(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 1:
            raise Exception("err")
    client.upload_file.side_effect = side_effect
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s", "region": "us-east-1"}
    upload_file(cfg, str(f), "k")
    mock_sleep.assert_called_once_with(_S3_RETRY_BACKOFF_BASE)


# ---------------------------------------------------------------------------
# generate_presigned_url — 4 tests
# ---------------------------------------------------------------------------
@patch("aurora.models.s3_storage._get_client")
def test_generate_presigned_url_default_expiry(mock_gc):
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://presigned"
    mock_gc.return_value = client
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    result = generate_presigned_url(cfg, "my/key")
    assert result == "https://presigned"
    client.generate_presigned_url.assert_called_once_with(
        "get_object", Params={"Bucket": "b", "Key": "my/key"}, ExpiresIn=3600,
    )


@patch("aurora.models.s3_storage._get_client")
def test_generate_presigned_url_custom_expiry(mock_gc):
    client = MagicMock()
    client.generate_presigned_url.return_value = "url"
    mock_gc.return_value = client
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    generate_presigned_url(cfg, "k", expires_in=7200)
    client.generate_presigned_url.assert_called_once_with(
        "get_object", Params={"Bucket": "b", "Key": "k"}, ExpiresIn=7200,
    )


@patch("aurora.models.s3_storage._get_client")
def test_generate_presigned_url_passes_config(mock_gc):
    client = MagicMock()
    client.generate_presigned_url.return_value = "u"
    mock_gc.return_value = client
    cfg = {"bucket": "bkt", "access_key": "ak", "secret_key": "sk", "region": "r"}
    generate_presigned_url(cfg, "key")
    mock_gc.assert_called_once_with(cfg)


@patch("aurora.models.s3_storage._get_client")
def test_generate_presigned_url_propagates_error(mock_gc):
    client = MagicMock()
    client.generate_presigned_url.side_effect = RuntimeError("denied")
    mock_gc.return_value = client
    cfg = {"bucket": "b", "access_key": "a", "secret_key": "s"}
    with pytest.raises(RuntimeError, match="denied"):
        generate_presigned_url(cfg, "k")
