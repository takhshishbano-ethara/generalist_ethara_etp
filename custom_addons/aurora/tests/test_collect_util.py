# -*- coding: utf-8 -*-
import os
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock


class TestAuroraPipelineError(TestCase):

    def test_is_exception(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        self.assertTrue(issubclass(AuroraPipelineError, Exception))

    def test_message_preserved(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        exc = AuroraPipelineError("test msg")
        self.assertEqual(str(exc), "test msg")

    def test_raise_catch(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            raise AuroraPipelineError("boom")


class TestConstants(TestCase):

    def test_rate_limit_floor(self):
        from odoo.addons.aurora.tools.collect.util import _RATE_LIMIT_FLOOR
        self.assertEqual(_RATE_LIMIT_FLOOR, 50)

    def test_rate_limit_sleep(self):
        from odoo.addons.aurora.tools.collect.util import _RATE_LIMIT_SLEEP_SECONDS
        self.assertEqual(_RATE_LIMIT_SLEEP_SECONDS, 30)

    def test_rate_limit_check_interval(self):
        from odoo.addons.aurora.tools.collect.util import _RATE_LIMIT_CHECK_INTERVAL
        self.assertEqual(_RATE_LIMIT_CHECK_INTERVAL, 100)


class TestTokenRotator(TestCase):

    def _mock_github(self, remaining=5000, reset_ts=9999999999):
        mock = MagicMock()
        mock.rate_limiting = (remaining, reset_ts)
        return mock

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_init_requires_tokens(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator, AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            TokenRotator([])

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_init_single_token(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        rotator = TokenRotator(["ghp_test"])
        self.assertEqual(rotator.token_count, 1)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_init_multiple_tokens(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        rotator = TokenRotator(["ghp_a", "ghp_b", "ghp_c"])
        self.assertEqual(rotator.token_count, 3)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_get_token_returns_string(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["ghp_test"])
        token = rotator.get_token()
        self.assertEqual(token, "ghp_test")

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_get_client_returns_github(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        mock_client = self._mock_github()
        MockGithub.return_value = mock_client
        rotator = TokenRotator(["ghp_test"])
        client = rotator.get_client()
        self.assertEqual(client, mock_client)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_round_robin(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["ghp_a", "ghp_b"])
        t1 = rotator.get_token()
        t2 = rotator.get_token()
        self.assertNotEqual(t1, t2)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_token_count_property(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        rotator = TokenRotator(["a", "b", "c"])
        self.assertEqual(rotator.token_count, 3)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_summary_returns_string(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["ghp_x"])
        s = rotator.summary()
        self.assertIsInstance(s, str)
        self.assertIn("token 1", s)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_get_rate_limits(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github(remaining=4500, reset_ts=1700000000)
        rotator = TokenRotator(["ghp_x"])
        limits = rotator.get_rate_limits()
        self.assertIsInstance(limits, dict)
        self.assertEqual(len(limits), 1)
        for h, info in limits.items():
            self.assertEqual(len(h), 64)
            self.assertEqual(info["remaining"], 4500)


class TestParseTokens(TestCase):

    def test_parse_list(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        result = parse_tokens(["ghp_a", "ghp_b"])
        self.assertEqual(result, ["ghp_a", "ghp_b"])

    def test_parse_string(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        result = parse_tokens("ghp_single")
        self.assertEqual(result, ["ghp_single"])

    def test_parse_path(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("ghp_file1\nghp_file2\n")
            path = f.name
        try:
            result = parse_tokens(Path(path))
            self.assertEqual(result, ["ghp_file1", "ghp_file2"])
        finally:
            os.unlink(path)

    def test_parse_path_nonexistent(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        with self.assertRaises(ValueError):
            parse_tokens(Path("/nonexistent/file.txt"))

    def test_parse_empty_list(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        self.assertEqual(parse_tokens([]), [])

    def test_parse_path_strips_whitespace(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("  ghp_ws  \n\n  ghp_ws2  \n")
            path = f.name
        try:
            result = parse_tokens(Path(path))
            self.assertEqual(result, ["ghp_ws", "ghp_ws2"])
        finally:
            os.unlink(path)


class TestOptionalInt(TestCase):

    def test_normal_int(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertEqual(optional_int("42"), 42)

    def test_none_string(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertIsNone(optional_int("none"))
        self.assertIsNone(optional_int("None"))
        self.assertIsNone(optional_int("NONE"))

    def test_null_string(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertIsNone(optional_int("null"))
        self.assertIsNone(optional_int("Null"))

    def test_empty_string(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertIsNone(optional_int(""))

    def test_invalid_raises(self):
        import argparse
        from odoo.addons.aurora.tools.collect.util import optional_int
        with self.assertRaises(argparse.ArgumentTypeError):
            optional_int("abc")

    def test_zero(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertEqual(optional_int("0"), 0)

    def test_negative(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertEqual(optional_int("-5"), -5)


class TestGetTokens(TestCase):

    @patch("odoo.addons.aurora.tools.collect.util._load_env_tokens", return_value=["ghp_env"])
    def test_get_tokens_from_env(self, mock_load):
        from odoo.addons.aurora.tools.collect.util import get_tokens
        result = get_tokens(None)
        self.assertEqual(result, ["ghp_env"])

    def test_get_tokens_from_list(self):
        from odoo.addons.aurora.tools.collect.util import get_tokens
        result = get_tokens(["ghp_a", "ghp_b"])
        self.assertEqual(len(result), 2)

    @patch("odoo.addons.aurora.tools.collect.util._load_env_tokens", return_value=[])
    @patch("odoo.addons.aurora.tools.collect.util.find_default_token_file", return_value=None)
    @patch.dict(os.environ, {}, clear=True)
    def test_get_tokens_no_source_raises(self, mock_find, mock_load):
        from odoo.addons.aurora.tools.collect.util import get_tokens, AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            get_tokens(None)


class TestFindDefaultTokenFile(TestCase):

    def test_returns_none_when_no_files(self):
        from odoo.addons.aurora.tools.collect.util import find_default_token_file
        with patch("odoo.addons.aurora.tools.collect.util.Path") as MockPath:
            mock_instance = MagicMock()
            mock_instance.exists.return_value = False
            MockPath.return_value = mock_instance
            MockPath.cwd.return_value = Path("/fake")


class TestLoadEnvTokens(TestCase):

    def test_loads_from_env_file(self):
        from odoo.addons.aurora.tools.collect.util import _load_env_tokens
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("GITHUB_TOKENS=ghp_aaa,ghp_bbb\n")
            with patch("odoo.addons.aurora.tools.collect.util.Path") as MockPath:
                MockPath.cwd.return_value = Path(tmpdir)
                result = _load_env_tokens()

    def test_skips_comments(self):
        from odoo.addons.aurora.tools.collect.util import _load_env_tokens
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("# comment\nGITHUB_TOKEN=ghp_test\n")
            with patch("odoo.addons.aurora.tools.collect.util.Path") as MockPath:
                MockPath.cwd.return_value = Path(tmpdir)
                result = _load_env_tokens()
