import hashlib
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from aurora.tools.util import (
    AuroraPipelineError,
    TokenRotator,
    _RATE_LIMIT_FLOOR,
)


class TestAuroraPipelineError:
    def test_is_exception_subclass(self):
        assert issubclass(AuroraPipelineError, Exception)

    def test_can_be_raised(self):
        with pytest.raises(AuroraPipelineError):
            raise AuroraPipelineError("boom")

    @pytest.mark.parametrize(
        "msg",
        [
            "simple error",
            "",
            "error with special chars !@#$%",
            "unicode: 日本語",
            "multi\nline\nerror",
            "a" * 10000,
        ],
        ids=["simple", "empty", "special", "unicode", "multiline", "long"],
    )
    def test_carries_message(self, msg):
        err = AuroraPipelineError(msg)
        assert str(err) == msg

    def test_can_be_caught_as_exception(self):
        try:
            raise AuroraPipelineError("test")
        except Exception as e:
            assert isinstance(e, AuroraPipelineError)


def _mock_github_class(remaining=5000, reset_ts=None):
    if reset_ts is None:
        reset_ts = int(time.time()) + 3600
    mock_gh = MagicMock()
    type(mock_gh).rate_limiting = PropertyMock(return_value=(remaining, reset_ts))
    return mock_gh


class TestTokenRotatorInit:
    def test_empty_list_raises(self):
        with pytest.raises(AuroraPipelineError, match="at least one token"):
            TokenRotator([])

    @pytest.mark.parametrize(
        "count",
        [1, 2, 3, 5, 10],
        ids=["one", "two", "three", "five", "ten"],
    )
    def test_accepts_n_tokens(self, count):
        tokens = [f"tok_{i}" for i in range(count)]
        tr = TokenRotator(tokens)
        assert tr.token_count == count

    def test_copies_input_list(self):
        tokens = ["a", "b"]
        tr = TokenRotator(tokens)
        tokens.append("c")
        assert tr.token_count == 2

    def test_initial_call_counts_zero(self):
        tr = TokenRotator(["t1", "t2"])
        assert all(v == 0 for v in tr._call_counts.values())


class TestTokenCount:
    @pytest.mark.parametrize(
        "n", [1, 2, 3, 5, 10, 20],
        ids=["1", "2", "3", "5", "10", "20"],
    )
    def test_token_count_property(self, n):
        tr = TokenRotator([f"t{i}" for i in range(n)])
        assert tr.token_count == n


class TestGetClient:
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_returns_github_instance(self, mock_token, mock_gh_cls):
        client = _mock_github_class()
        mock_gh_cls.return_value = client
        tr = TokenRotator(["tok1"])
        result = tr.get_client()
        assert result is client

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_increments_call_count(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["tok1"])
        tr.get_client()
        tr.get_client()
        assert tr._call_counts[0] == 2

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_rotates_between_tokens(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["t1", "t2", "t3"])
        tr.get_client()
        tr.get_client()
        tr.get_client()
        assert sum(tr._call_counts.values()) == 3

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_caches_client(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["tok1"])
        c1 = tr.get_client()
        c2 = tr.get_client()
        assert mock_gh_cls.call_count == 1

    @pytest.mark.parametrize(
        "num_tokens, num_calls",
        [(1, 5), (2, 6), (3, 9), (5, 10)],
        ids=["1tok-5calls", "2tok-6calls", "3tok-9calls", "5tok-10calls"],
    )
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_total_calls_tracked(self, mock_token, mock_gh_cls, num_tokens, num_calls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator([f"t{i}" for i in range(num_tokens)])
        for _ in range(num_calls):
            tr.get_client()
        assert sum(tr._call_counts.values()) == num_calls


class TestGetToken:
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_returns_string_token(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["ghp_abc123"])
        tok = tr.get_token()
        assert tok == "ghp_abc123"

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_increments_call_count(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["tok1"])
        tr.get_token()
        assert tr._call_counts[0] == 1

    @pytest.mark.parametrize(
        "num_tokens",
        [1, 2, 3, 5],
        ids=["1tok", "2tok", "3tok", "5tok"],
    )
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_returns_valid_token_from_pool(self, mock_token, mock_gh_cls, num_tokens):
        mock_gh_cls.return_value = _mock_github_class()
        tokens = [f"tok_{i}" for i in range(num_tokens)]
        tr = TokenRotator(tokens)
        result = tr.get_token()
        assert result in tokens

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_rotates_tokens(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["a", "b"])
        t1 = tr.get_token()
        t2 = tr.get_token()
        assert {t1, t2} == {"a", "b"}


class TestSummary:
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_format_single_token(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class(remaining=4999)
        tr = TokenRotator(["tok1"])
        tr.get_client()
        s = tr.summary()
        assert "token 1" in s
        assert "4999 remaining" in s
        assert "1 calls" in s

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_format_multiple_tokens(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class(remaining=3000)
        tr = TokenRotator(["t1", "t2", "t3"])
        s = tr.summary()
        assert "token 1" in s
        assert "token 2" in s
        assert "token 3" in s

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_summary_lines_match_token_count(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["a", "b", "c", "d"])
        lines = tr.summary().strip().split("\n")
        assert len(lines) == 4


class TestGetRateLimits:
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_returns_dict(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class(remaining=100, reset_ts=9999)
        tr = TokenRotator(["tok1"])
        result = tr.get_rate_limits()
        assert isinstance(result, dict)
        assert len(result) == 1

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_keys_are_sha256_hashes(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["tok1"])
        result = tr.get_rate_limits()
        expected_hash = hashlib.sha256(b"tok1").hexdigest()
        assert expected_hash in result

    @pytest.mark.parametrize(
        "num_tokens",
        [1, 2, 3, 5],
        ids=["1tok", "2tok", "3tok", "5tok"],
    )
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_entry_count_matches_tokens(self, mock_token, mock_gh_cls, num_tokens):
        mock_gh_cls.return_value = _mock_github_class()
        tokens = [f"tok_{i}" for i in range(num_tokens)]
        tr = TokenRotator(tokens)
        result = tr.get_rate_limits()
        assert len(result) == num_tokens

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_entry_structure(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class(remaining=42, reset_ts=12345)
        tr = TokenRotator(["mytoken"])
        result = tr.get_rate_limits()
        h = hashlib.sha256(b"mytoken").hexdigest()
        assert result[h]["remaining"] == 42
        assert result[h]["reset"] == 12345

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_unique_hashes_for_different_tokens(self, mock_token, mock_gh_cls):
        mock_gh_cls.return_value = _mock_github_class()
        tr = TokenRotator(["aaa", "bbb"])
        result = tr.get_rate_limits()
        assert len(result) == 2
        hashes = list(result.keys())
        assert hashes[0] != hashes[1]


class TestRateLimitBehavior:
    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_skips_exhausted_token(self, mock_token, mock_gh_cls):
        exhausted = _mock_github_class(remaining=0)
        healthy = _mock_github_class(remaining=5000)
        clients = [exhausted, healthy]
        mock_gh_cls.side_effect = clients
        tr = TokenRotator(["bad", "good"])
        tok = tr.get_token()
        assert tok == "good"

    @patch("aurora.tools.util.Github")
    @patch("aurora.tools.util.Auth.Token")
    def test_skips_near_limit_token(self, mock_token, mock_gh_cls):
        near_limit = _mock_github_class(remaining=_RATE_LIMIT_FLOOR)
        healthy = _mock_github_class(remaining=5000)
        mock_gh_cls.side_effect = [near_limit, healthy]
        tr = TokenRotator(["low", "high"])
        tok = tr.get_token()
        assert tok == "high"
