# -*- coding: utf-8 -*-
import os
import sys
from unittest.mock import patch, MagicMock

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


def _get_mock_repo(
    full_name="arrow-py/arrow",
    stars=8000,
    fork=False,
    archived=False,
    size=15000,
    language="Python",
    homepage="https://arrow.readthedocs.io",
    topics=None,
    description="Better dates & times for Python",
):
    return {
        "full_name": full_name,
        "fork": fork,
        "archived": archived,
        "stargazers_count": stars,
        "size": size,
        "language": language,
        "homepage": homepage,
        "topics": topics or [],
        "description": description,
    }


def _get_mock_root_contents():
    return [
        {"name": "arrow", "type": "dir"},
        {"name": "tests", "type": "dir"},
        {"name": "docs", "type": "dir"},
        {"name": "pyproject.toml", "type": "file"},
        {"name": "README.rst", "type": "file"},
        {"name": "LICENSE", "type": "file"},
    ]


def _ensure_tools_on_path():
    module_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools_path = os.path.join(module_path, "tools")
    parent = os.path.dirname(tools_path)
    if parent not in sys.path:
        sys.path.insert(0, parent)


@tagged("post_install", "-at_install")
class TestRepoValidatorChecks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_tools_on_path()
        from tools.repo_validator import (
            _check_not_fork,
            _check_not_archived,
            _check_stars,
            _check_repo_size,
            _check_not_ml_framework,
            _check_not_cli_tool,
            _check_not_native_wrapper,
        )

        cls._check_not_fork = staticmethod(_check_not_fork)
        cls._check_not_archived = staticmethod(_check_not_archived)
        cls._check_stars = staticmethod(_check_stars)
        cls._check_repo_size = staticmethod(_check_repo_size)
        cls._check_not_ml_framework = staticmethod(_check_not_ml_framework)
        cls._check_not_cli_tool = staticmethod(_check_not_cli_tool)
        cls._check_not_native_wrapper = staticmethod(_check_not_native_wrapper)

    def test_not_fork_passes(self):
        ok, _ = self._check_not_fork(_get_mock_repo(fork=False))
        self.assertTrue(ok)

    def test_not_fork_fails(self):
        ok, reason = self._check_not_fork(_get_mock_repo(fork=True))
        self.assertFalse(ok)
        self.assertIn("fork", reason.lower())

    def test_not_archived_passes(self):
        ok, _ = self._check_not_archived(_get_mock_repo(archived=False))
        self.assertTrue(ok)

    def test_not_archived_fails(self):
        ok, _ = self._check_not_archived(_get_mock_repo(archived=True))
        self.assertFalse(ok)

    def test_stars_passes_at_threshold(self):
        ok, _ = self._check_stars(_get_mock_repo(stars=5000), threshold=5000)
        self.assertTrue(ok)

    def test_stars_fails_below_threshold(self):
        ok, reason = self._check_stars(_get_mock_repo(stars=2999), threshold=3000)
        self.assertFalse(ok)
        self.assertIn("2999", reason)

    def test_repo_size_passes(self):
        ok, _ = self._check_repo_size(_get_mock_repo(size=50000))
        self.assertTrue(ok)

    def test_repo_size_fails_over_500mb(self):
        ok, _ = self._check_repo_size(_get_mock_repo(size=600000))
        self.assertFalse(ok)

    def test_ml_framework_rejected(self):
        ok, reason = self._check_not_ml_framework(
            _get_mock_repo(topics=["machine-learning", "python"])
        )
        self.assertFalse(ok)
        self.assertIn("machine-learning", reason)

    def test_ml_framework_description_rejected(self):
        ok, _ = self._check_not_ml_framework(
            _get_mock_repo(description="A deep-learning framework for NLP")
        )
        self.assertFalse(ok)

    def test_clean_repo_passes_ml_check(self):
        ok, _ = self._check_not_ml_framework(
            _get_mock_repo(topics=["python", "datetime"], description="Date library")
        )
        self.assertTrue(ok)

    def test_cli_tool_rejected(self):
        ok, _ = self._check_not_cli_tool(_get_mock_repo(topics=["cli", "python"]))
        self.assertFalse(ok)

    def test_native_wrapper_rejected(self):
        ok, _ = self._check_not_native_wrapper(
            _get_mock_repo(description="Python binding for libfoo")
        )
        self.assertFalse(ok)

    def test_clean_repo_passes_native_check(self):
        ok, _ = self._check_not_native_wrapper(
            _get_mock_repo(description="A pure-python date library")
        )
        self.assertTrue(ok)


@tagged("post_install", "-at_install")
class TestRepoValidatorAPIChecks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_tools_on_path()

    @patch("tools.repo_validator._get_languages")
    def test_python_ratio_passes(self, mock_langs):
        from tools.repo_validator import _check_python_ratio

        mock_langs.return_value = {"Python": 95000, "Shell": 5000}
        ok, reason = _check_python_ratio("arrow-py/arrow")
        self.assertTrue(ok)
        self.assertIn("95.0%", reason)

    @patch("tools.repo_validator._get_languages")
    def test_python_ratio_fails(self, mock_langs):
        from tools.repo_validator import _check_python_ratio

        mock_langs.return_value = {"Python": 50000, "JavaScript": 50000}
        ok, reason = _check_python_ratio("some/repo")
        self.assertFalse(ok)
        self.assertIn("50.00%", reason)

    @patch("tools.repo_validator._get_file_content")
    def test_uses_pytest_via_pyproject(self, mock_content):
        from tools.repo_validator import _check_uses_pytest

        def side_effect(fn, path, ref="HEAD"):
            if path == "pyproject.toml":
                return '[tool.pytest.ini_options]\naddopts = "-v"'
            return None

        mock_content.side_effect = side_effect
        with patch("tools.repo_validator._file_exists", return_value=False):
            ok, reason = _check_uses_pytest("arrow-py/arrow")
            self.assertTrue(ok)
            self.assertIn("pyproject.toml", reason)

    @patch("tools.repo_validator._get_file_content")
    def test_no_gpu_passes(self, mock_content):
        from tools.repo_validator import _check_no_gpu_usage

        mock_content.return_value = "requests\narrow\npython-dateutil"
        ok, _ = _check_no_gpu_usage("arrow-py/arrow")
        self.assertTrue(ok)

    @patch("tools.repo_validator._get_file_content")
    def test_gpu_detected(self, mock_content):
        from tools.repo_validator import _check_no_gpu_usage

        def side_effect(fn, path, ref="HEAD"):
            if path == "requirements.txt":
                return "torch\ncupy-cuda12x\nnumpy"
            return None

        mock_content.side_effect = side_effect
        ok, reason = _check_no_gpu_usage("some/repo")
        self.assertFalse(ok)
        self.assertIn("GPU", reason)

    @patch("tools.repo_validator._file_exists")
    @patch("tools.repo_validator._get_file_content")
    def test_installable_with_pyproject(self, mock_content, mock_exists):
        from tools.repo_validator import _check_installable

        mock_exists.return_value = True
        mock_content.return_value = '[build-system]\nrequires = ["setuptools"]'
        ok, reason = _check_installable("arrow-py/arrow")
        self.assertTrue(ok)
        self.assertIn("pyproject.toml", reason)

    @patch("tools.repo_validator._get_file_content")
    def test_python_version_compat_passes(self, mock_content):
        from tools.repo_validator import _check_python_version_compat

        mock_content.return_value = 'requires-python = ">=3.9"'
        ok, _ = _check_python_version_compat("arrow-py/arrow")
        self.assertTrue(ok)

    @patch("tools.repo_validator._get_file_content")
    def test_python_version_compat_fails_old(self, mock_content):
        from tools.repo_validator import _check_python_version_compat

        def side_effect(fn, path, ref="HEAD"):
            if path == "pyproject.toml":
                return 'requires-python = "<=3.7"'
            return None

        mock_content.side_effect = side_effect
        ok, reason = _check_python_version_compat("old/repo")
        self.assertFalse(ok)
        self.assertIn("too old", reason)


@tagged("post_install", "-at_install")
class TestRepoValidatorIntegration(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_tools_on_path()

    @patch("tools.repo_validator.github_get_json")
    @patch("tools.repo_validator._get_languages")
    @patch("tools.repo_validator._get_repo_contents")
    @patch("tools.repo_validator._get_file_content")
    @patch("tools.repo_validator._file_exists")
    @patch("tools.repo_validator._get_session")
    def test_full_validation_pass(
        self, mock_session, mock_exists, mock_content, mock_root, mock_langs, mock_api
    ):
        from tools.repo_validator import validate_repo

        mock_api.return_value = _get_mock_repo()
        mock_langs.return_value = {"Python": 98000, "Shell": 2000}
        mock_root.return_value = _get_mock_root_contents()

        def file_exists_side_effect(fn, path):
            return path in (
                "pyproject.toml",
                "conftest.py",
                "docs",
                "arrow/__init__.py",
            )

        mock_exists.side_effect = file_exists_side_effect

        def content_side_effect(fn, path, ref="HEAD"):
            if path == "pyproject.toml":
                return (
                    '[build-system]\nrequires = ["setuptools"]\n'
                    '[tool.pytest.ini_options]\naddopts = "-v"\n'
                    'requires-python = ">=3.9"'
                )
            return None

        mock_content.side_effect = content_side_effect

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_sess = MagicMock()
        mock_sess.head.return_value = mock_resp
        mock_sess.get.return_value = mock_resp
        mock_session.return_value = mock_sess

        result = validate_repo("arrow-py/arrow", github_token="fake-token")

        self.assertTrue(result["filter1_passed"])
        self.assertTrue(result["filter2_passed"])
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["filter2_score"], 2)
        self.assertIn("PASSED", result["summary"])

    @patch("tools.repo_validator.github_get_json")
    def test_full_validation_fork_fails(self, mock_api):
        from tools.repo_validator import validate_repo

        mock_api.return_value = _get_mock_repo(fork=True)

        result = validate_repo("some/fork-repo", github_token="fake")
        self.assertFalse(result["filter1_passed"])
        self.assertFalse(result["passed"])

    @patch("tools.repo_validator.github_get_json")
    def test_full_validation_api_error(self, mock_api):
        from tools.repo_validator import validate_repo

        mock_api.side_effect = RuntimeError("404 Not Found")

        result = validate_repo("nonexistent/repo")
        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["error"])
        self.assertIn("Cannot access", result["error"])


@tagged("post_install", "-at_install")
class TestPipelineRunValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.PipelineRun = self.env["commit0.pipeline.run"]

    def test_validate_requires_single_mode(self):
        run = self.PipelineRun.create({"entry_type": "batch"})
        with self.assertRaises(ValidationError):
            run.action_validate_repo()

    def test_validate_requires_repo_url(self):
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        run.repo_url = False
        with self.assertRaises(ValidationError):
            run.action_validate_repo()

    @patch("tools.repo_validator.validate_repo")
    @patch("tools.repo_validator.format_validation_report")
    def test_validate_sets_passed(self, mock_format, mock_validate):
        _ensure_tools_on_path()
        mock_validate.return_value = {
            "passed": True,
            "filter1_passed": True,
            "filter2_passed": True,
            "filter2_score": 4,
            "checks": [],
            "repo_info": {},
            "summary": "PASSED all checks",
            "error": None,
        }
        mock_format.return_value = "All checks passed"

        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        run.action_validate_repo()
        self.assertEqual(run.validation_status, "passed")
        self.assertEqual(run.validation_details, "All checks passed")

    @patch("tools.repo_validator.validate_repo")
    @patch("tools.repo_validator.format_validation_report")
    def test_validate_sets_failed(self, mock_format, mock_validate):
        _ensure_tools_on_path()
        mock_validate.return_value = {
            "passed": False,
            "filter1_passed": False,
            "filter2_passed": False,
            "filter2_score": 0,
            "checks": [("Not a fork", False, "Is a fork")],
            "repo_info": {},
            "summary": "FAILED Filter 1: Not a fork",
            "error": None,
        }
        mock_format.return_value = "[FAIL] Not a fork — Is a fork"

        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/some/forked-repo",
            }
        )
        run.action_validate_repo()
        self.assertEqual(run.validation_status, "failed")
        self.assertIn("FAIL", run.validation_details)

    @patch("tools.repo_validator.validate_repo")
    def test_validate_handles_exception(self, mock_validate):
        _ensure_tools_on_path()
        mock_validate.side_effect = ConnectionError("Network unreachable")

        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        run.action_validate_repo()
        self.assertEqual(run.validation_status, "failed")
        self.assertIn("Network unreachable", run.validation_details)

    def test_validate_strips_git_suffix(self):
        _ensure_tools_on_path()
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/dry-python/returns.git",
            }
        )
        with patch("tools.repo_validator.validate_repo") as mock_validate:
            mock_validate.return_value = {
                "passed": True,
                "filter1_passed": True,
                "filter2_passed": True,
                "filter2_score": 3,
                "checks": [],
                "repo_info": {},
                "summary": "PASSED",
                "error": None,
            }
            with patch(
                "tools.repo_validator.format_validation_report", return_value="OK"
            ):
                run.action_validate_repo()
                mock_validate.assert_called_once_with(
                    "dry-python/returns", github_token=""
                )

    def test_default_validation_status(self):
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        self.assertEqual(run.validation_status, "not_validated")
