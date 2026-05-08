# -*- coding: utf-8 -*-
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open


class TestRegistryWizardToClassName(unittest.TestCase):
    """Tests for registry_wizard._to_class_name"""

    def test_simple_repo_name(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("requests"), "Requests")

    def test_hyphenated_repo(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("my-repo"), "MyRepo")

    def test_underscored_repo(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("my_repo"), "MyRepo")

    def test_mixed_separators(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("my-awesome_repo"), "MyAwesomeRepo")

    def test_dotted_repo(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("vue.js")
        self.assertNotIn(".", result)

    def test_multiple_hyphens(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("a-b-c-d"), "ABCD")

    def test_numbers_preserved(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("lib2to3")
        self.assertIn("2", result)

    def test_already_camel_case(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("FastAPI")
        self.assertTrue(result[0].isupper())

    def test_empty_string(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name(""), "")

    def test_single_char(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        self.assertEqual(_to_class_name("x"), "X")

    def test_all_special_chars(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("---")
        self.assertEqual(result, "")

    def test_repo_with_digits_and_hyphens(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("jackson-databind-2")
        self.assertIn("Jackson", result)

    def test_uppercase_input(self):
        from odoo.addons.aurora.models.registry_wizard import _to_class_name
        result = _to_class_name("ALLCAPS")
        self.assertTrue(result[0].isupper())


class TestRegistryWizardTemplate(unittest.TestCase):
    """Tests for registry_wizard._TEMPLATE format string"""

    def test_template_has_class_name_placeholder(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("{class_name}", _TEMPLATE)

    def test_template_has_org_placeholder(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("{org}", _TEMPLATE)

    def test_template_has_repo_placeholder(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("{repo}", _TEMPLATE)

    def test_template_contains_instance_register(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("Instance.register(", _TEMPLATE)

    def test_template_contains_image_base_class(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("ImageBase(Image)", _TEMPLATE)

    def test_template_contains_image_default_class(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("ImageDefault(Image)", _TEMPLATE)

    def test_template_contains_parse_log(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        self.assertIn("def parse_log(", _TEMPLATE)

    def test_template_format_substitution(self):
        from odoo.addons.aurora.models.registry_wizard import _TEMPLATE
        result = _TEMPLATE.format(class_name="TestLib", org="myorg", repo="myrepo")
        self.assertIn("class TestLibImageBase", result)
        self.assertIn("class TestLib(Instance)", result)


class TestRegistryWizardFindSample(unittest.TestCase):
    """Tests for registry_wizard._find_sample_registry"""

    @patch("odoo.addons.aurora.models.registry_wizard._AURORA_HARNESS_REPOS_ROOT")
    @patch("odoo.addons.aurora.models.registry_wizard._HARNESS_REPOS_ROOT")
    def test_returns_empty_when_no_dirs(self, mock_root, mock_aurora_root):
        from odoo.addons.aurora.models.registry_wizard import _find_sample_registry
        mock_root.__truediv__ = MagicMock(return_value=MagicMock(is_dir=MagicMock(return_value=False)))
        mock_aurora_root.__truediv__ = MagicMock(return_value=MagicMock(is_dir=MagicMock(return_value=False)))
        result = _find_sample_registry("nonexistent_lang")
        self.assertEqual(result, "")

    def test_function_signature_accepts_lang(self):
        from odoo.addons.aurora.models.registry_wizard import _find_sample_registry
        import inspect
        sig = inspect.signature(_find_sample_registry)
        self.assertIn("lang", sig.parameters)

    def test_function_signature_accepts_exclude_org(self):
        from odoo.addons.aurora.models.registry_wizard import _find_sample_registry
        import inspect
        sig = inspect.signature(_find_sample_registry)
        self.assertIn("exclude_org", sig.parameters)

    def test_function_signature_accepts_exclude_repo(self):
        from odoo.addons.aurora.models.registry_wizard import _find_sample_registry
        import inspect
        sig = inspect.signature(_find_sample_registry)
        self.assertIn("exclude_repo", sig.parameters)


class TestRegistryWizardGenerateLlmPrompt(unittest.TestCase):
    """Tests for registry_wizard._generate_llm_prompt"""

    def test_prompt_contains_org_repo(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("testorg", "testrepo", "python", "sample code")
        self.assertIn("testorg/testrepo", result)

    def test_prompt_contains_language(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "java", "sample")
        self.assertIn("java", result)

    def test_prompt_contains_sample_content(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        sample = "class SampleImageBase(Image): pass"
        result = _generate_llm_prompt("org", "repo", "python", sample)
        self.assertIn(sample, result)

    def test_prompt_contains_test_framework_hint(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "python", "")
        self.assertIn("pytest", result)

    def test_prompt_javascript_frameworks(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "javascript", "")
        self.assertIn("jest", result)

    def test_prompt_unknown_lang_fallback(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "brainfuck", "")
        self.assertIn("standard test runner", result)

    def test_prompt_golang_frameworks(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "golang", "")
        self.assertIn("go test", result)

    def test_prompt_returns_string(self):
        from odoo.addons.aurora.models.registry_wizard import _generate_llm_prompt
        result = _generate_llm_prompt("org", "repo", "rust", "")
        self.assertIsInstance(result, str)


class TestRegistryWizardHarnessRoot(unittest.TestCase):
    """Tests for _HARNESS_REPOS_ROOT path"""

    def test_harness_repos_root_is_path(self):
        from odoo.addons.aurora.models.registry_wizard import _HARNESS_REPOS_ROOT
        from pathlib import Path
        self.assertIsInstance(_HARNESS_REPOS_ROOT, Path)

    def test_harness_repos_root_ends_with_repos(self):
        from odoo.addons.aurora.models.registry_wizard import _HARNESS_REPOS_ROOT
        self.assertEqual(_HARNESS_REPOS_ROOT.name, "repos")

    def test_harness_repos_root_contains_harness(self):
        from odoo.addons.aurora.models.registry_wizard import _HARNESS_REPOS_ROOT
        self.assertIn("harness", str(_HARNESS_REPOS_ROOT))

    def test_aurora_harness_repos_root_is_path(self):
        from odoo.addons.aurora.models.registry_wizard import _AURORA_HARNESS_REPOS_ROOT
        from pathlib import Path
        self.assertIsInstance(_AURORA_HARNESS_REPOS_ROOT, Path)


class TestRegistryGitSync(unittest.TestCase):
    """Tests for registry_git_sync module"""

    def test_build_path_format(self):
        from odoo.addons.aurora.models.registry_git_sync import _build_path
        result = _build_path("python", "myorg", "myrepo.py")
        self.assertEqual(result, "multi_swe_bench/harness/repos/python/myorg/myrepo.py")

    def test_build_path_includes_prefix(self):
        from odoo.addons.aurora.models.registry_git_sync import _build_path
        result = _build_path("java", "org", "file.py")
        self.assertTrue(result.startswith("multi_swe_bench/harness/repos/"))

    def test_default_repo_slug(self):
        from odoo.addons.aurora.models.registry_git_sync import _DEFAULT_REPO_SLUG
        self.assertEqual(_DEFAULT_REPO_SLUG, "EtharaAI/multi-swe-bench")

    def test_default_branch(self):
        from odoo.addons.aurora.models.registry_git_sync import _DEFAULT_BRANCH
        self.assertEqual(_DEFAULT_BRANCH, "main")

    def test_default_path_prefix(self):
        from odoo.addons.aurora.models.registry_git_sync import _DEFAULT_PATH_PREFIX
        self.assertEqual(_DEFAULT_PATH_PREFIX, "multi_swe_bench/harness/repos")

    @patch("odoo.addons.aurora.models.registry_git_sync.get_encrypted_param")
    def test_push_returns_none_when_no_token(self, mock_get):
        from odoo.addons.aurora.models.registry_git_sync import push_registry_to_github
        mock_get.return_value = None
        env = MagicMock()
        env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=MagicMock())))
        result = push_registry_to_github(env, "python", "org", "file.py", "content", "msg")
        self.assertIsNone(result)

    @patch("odoo.addons.aurora.models.registry_git_sync.get_encrypted_param")
    def test_push_with_full_path_overrides_build(self, mock_get):
        from odoo.addons.aurora.models.registry_git_sync import push_registry_to_github
        mock_get.return_value = None
        env = MagicMock()
        env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=MagicMock())))
        result = push_registry_to_github(
            env, "python", "org", "file.py", "content", "msg",
            full_path="custom/path.py"
        )
        self.assertIsNone(result)

    @patch("odoo.addons.aurora.models.registry_git_sync.get_encrypted_param")
    def test_ensure_init_returns_empty_when_no_token(self, mock_get):
        from odoo.addons.aurora.models.registry_git_sync import ensure_init_files_on_github
        mock_get.return_value = None
        env = MagicMock()
        env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=MagicMock())))
        result = ensure_init_files_on_github(env, "python", "org", "repo")
        self.assertEqual(result, [])


class TestHarnessStagingStates(unittest.TestCase):
    """Tests for harness_staging.py states and constants"""

    def test_staging_stage_selection_has_draft(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_STAGE_SELECTION
        keys = [s[0] for s in STAGING_STAGE_SELECTION]
        self.assertIn("draft", keys)

    def test_staging_stage_selection_has_testing(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_STAGE_SELECTION
        keys = [s[0] for s in STAGING_STAGE_SELECTION]
        self.assertIn("testing", keys)

    def test_staging_stage_selection_has_deployed(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_STAGE_SELECTION
        keys = [s[0] for s in STAGING_STAGE_SELECTION]
        self.assertIn("deployed", keys)

    def test_staging_stage_selection_has_failed(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_STAGE_SELECTION
        keys = [s[0] for s in STAGING_STAGE_SELECTION]
        self.assertIn("failed", keys)

    def test_staging_stage_selection_count(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_STAGE_SELECTION
        self.assertEqual(len(STAGING_STAGE_SELECTION), 8)

    def test_staging_test_result_has_idle(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_TEST_RESULT
        keys = [s[0] for s in STAGING_TEST_RESULT]
        self.assertIn("idle", keys)

    def test_staging_test_result_has_success(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_TEST_RESULT
        keys = [s[0] for s in STAGING_TEST_RESULT]
        self.assertIn("success", keys)

    def test_staging_test_result_has_failed(self):
        from odoo.addons.aurora.models.harness_staging import STAGING_TEST_RESULT
        keys = [s[0] for s in STAGING_TEST_RESULT]
        self.assertIn("failed", keys)

    def test_required_instance_methods(self):
        from odoo.addons.aurora.models.harness_staging import _REQUIRED_INSTANCE_METHODS
        self.assertIn("run", _REQUIRED_INSTANCE_METHODS)
        self.assertIn("parse_log", _REQUIRED_INSTANCE_METHODS)

    def test_required_image_methods(self):
        from odoo.addons.aurora.models.harness_staging import _REQUIRED_IMAGE_METHODS
        self.assertIn("dependency", _REQUIRED_IMAGE_METHODS)
        self.assertIn("dockerfile", _REQUIRED_IMAGE_METHODS)

    def test_language_selection_has_python(self):
        from odoo.addons.aurora.models.harness_staging import LANGUAGE_SELECTION
        keys = [s[0] for s in LANGUAGE_SELECTION]
        self.assertIn("python", keys)

    def test_language_selection_has_golang(self):
        from odoo.addons.aurora.models.harness_staging import LANGUAGE_SELECTION
        keys = [s[0] for s in LANGUAGE_SELECTION]
        self.assertIn("golang", keys)

    def test_is_zip_upload_true_for_zip(self):
        obj = MagicMock()
        obj.harness_filename = "registry.zip"
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        result = AuroraHarnessStaging._is_zip_upload(obj)
        self.assertTrue(result)

    def test_is_zip_upload_false_for_py(self):
        obj = MagicMock()
        obj.harness_filename = "registry.py"
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        result = AuroraHarnessStaging._is_zip_upload(obj)
        self.assertFalse(result)

    def test_is_zip_upload_false_for_none(self):
        obj = MagicMock()
        obj.harness_filename = None
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        result = AuroraHarnessStaging._is_zip_upload(obj)
        self.assertFalse(result)

    def test_is_zip_upload_false_for_empty(self):
        obj = MagicMock()
        obj.harness_filename = ""
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        result = AuroraHarnessStaging._is_zip_upload(obj)
        self.assertFalse(result)


class TestHarnessStagingValidation(unittest.TestCase):
    """Tests for harness file content validation"""

    def test_validate_valid_content(self):
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        valid_content = b'''
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.image import Image

class TestImageBase(Image):
    def dependency(self): pass
    def files(self): pass
    def dockerfile(self): pass

@Instance.register("org", "repo")
class Test(Instance):
    def run(self): pass
    def test_patch_run(self): pass
    def fix_patch_run(self): pass
    def parse_log(self, log): pass
'''
        obj = MagicMock()
        AuroraHarnessStaging._validate_harness_content(obj, valid_content)

    def test_validate_invalid_utf8_raises(self):
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        from odoo.exceptions import UserError
        obj = MagicMock()
        with self.assertRaises(UserError):
            AuroraHarnessStaging._validate_harness_content(obj, b'\xff\xfe invalid')

    def test_validate_syntax_error_raises(self):
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        from odoo.exceptions import UserError
        obj = MagicMock()
        with self.assertRaises(UserError):
            AuroraHarnessStaging._validate_harness_content(obj, b"def broken(")

    def test_validate_missing_register_raises(self):
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        from odoo.exceptions import UserError
        content = b'''
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.image import Image

class TestImageBase(Image):
    def dependency(self): pass
    def files(self): pass
    def dockerfile(self): pass

class Test(Instance):
    def run(self): pass
    def test_patch_run(self): pass
    def fix_patch_run(self): pass
    def parse_log(self, log): pass
'''
        obj = MagicMock()
        with self.assertRaises(UserError):
            AuroraHarnessStaging._validate_harness_content(obj, content)

    def test_validate_missing_instance_methods_raises(self):
        from odoo.addons.aurora.models.harness_staging import AuroraHarnessStaging
        from odoo.exceptions import UserError
        content = b'''
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.image import Image

class TestImageBase(Image):
    def dependency(self): pass
    def files(self): pass
    def dockerfile(self): pass

@Instance.register("org", "repo")
class Test(Instance):
    def run(self): pass
'''
        obj = MagicMock()
        with self.assertRaises(UserError):
            AuroraHarnessStaging._validate_harness_content(obj, content)


class TestHarnessStagingExecutor(unittest.TestCase):
    """Tests for harness_staging_executor.py"""

    def test_max_staging_threads_is_one(self):
        from odoo.addons.aurora.models.harness_staging_executor import _MAX_STAGING_THREADS
        self.assertEqual(_MAX_STAGING_THREADS, 1)

    def test_max_concurrent_tests_is_one(self):
        from odoo.addons.aurora.models.harness_staging_executor import _MAX_CONCURRENT_TESTS
        self.assertEqual(_MAX_CONCURRENT_TESTS, 1)

    def test_allowed_columns_frozen(self):
        from odoo.addons.aurora.models.harness_staging_executor import _ALLOWED_COLUMNS
        self.assertIsInstance(_ALLOWED_COLUMNS, frozenset)

    def test_allowed_columns_has_stage(self):
        from odoo.addons.aurora.models.harness_staging_executor import _ALLOWED_COLUMNS
        self.assertIn("stage", _ALLOWED_COLUMNS)

    def test_allowed_columns_has_test_result(self):
        from odoo.addons.aurora.models.harness_staging_executor import _ALLOWED_COLUMNS
        self.assertIn("test_result", _ALLOWED_COLUMNS)

    def test_allowed_columns_has_test_log(self):
        from odoo.addons.aurora.models.harness_staging_executor import _ALLOWED_COLUMNS
        self.assertIn("test_log", _ALLOWED_COLUMNS)

    def test_max_log_size(self):
        from odoo.addons.aurora.models.harness_staging_executor import _MAX_LOG_SIZE
        self.assertEqual(_MAX_LOG_SIZE, 2_000_000)

    def test_update_staging_rejects_invalid_columns(self):
        from odoo.addons.aurora.models.harness_staging_executor import _update_staging
        mock_cr = MagicMock()
        with self.assertRaises(ValueError):
            _update_staging(mock_cr, 1, {"invalid_col": "value"})

    def test_update_staging_empty_vals(self):
        from odoo.addons.aurora.models.harness_staging_executor import _update_staging
        mock_cr = MagicMock()
        _update_staging(mock_cr, 1, {})
        mock_cr.execute.assert_not_called()

    def test_update_staging_valid_columns(self):
        from odoo.addons.aurora.models.harness_staging_executor import _update_staging
        mock_cr = MagicMock()
        _update_staging(mock_cr, 1, {"stage": "testing"})
        mock_cr.execute.assert_called_once()

    def test_append_test_log_format(self):
        from odoo.addons.aurora.models.harness_staging_executor import _append_test_log
        mock_cr = MagicMock()
        _append_test_log(mock_cr, 42, "test message")
        mock_cr.execute.assert_called_once()
        call_args = mock_cr.execute.call_args
        self.assertIn("test message", call_args[0][1][0])

    def test_is_test_slot_available_returns_bool(self):
        from odoo.addons.aurora.models.harness_staging_executor import is_test_slot_available
        result = is_test_slot_available()
        self.assertIsInstance(result, bool)

    def test_submit_test_async_signature(self):
        from odoo.addons.aurora.models.harness_staging_executor import submit_test_async
        import inspect
        sig = inspect.signature(submit_test_async)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["db_name", "uid", "rec_id"])


class TestPipelineResult(unittest.TestCase):
    """Tests for pipeline_result.py model structure"""

    def test_model_name(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertEqual(AuroraPipelineResult._name, "aurora.pipeline.result")

    def test_model_description(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertEqual(AuroraPipelineResult._description, "Aurora Phase 2 Instance Result")

    def test_model_order(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertEqual(AuroraPipelineResult._order, "sequence, id")

    def test_has_compute_run_total(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "_compute_run_total"))

    def test_has_compute_test_total(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "_compute_test_total"))

    def test_has_compute_fix_total(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "_compute_fix_total"))

    def test_has_compute_status_icon(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "_compute_status_icon"))

    def test_pr_attribution_method_choices(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "pr_attribution_method"))

    def test_version_scheme_choices(self):
        from odoo.addons.aurora.models.pipeline_result import AuroraPipelineResult
        self.assertTrue(hasattr(AuroraPipelineResult, "version_scheme"))


class TestPoolMetricsStructure(unittest.TestCase):
    """Tests for pool_metrics.py model - edge cases not in existing tests"""

    def test_model_name_constant(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertEqual(AuroraPoolMetrics._name, "aurora.pool.metrics")

    def test_model_description(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertEqual(AuroraPoolMetrics._description, "Token Pool Metrics Snapshot")

    def test_model_ordering(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertEqual(AuroraPoolMetrics._order, "timestamp desc")

    def test_has_timestamp_field(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertTrue(hasattr(AuroraPoolMetrics, "timestamp"))

    def test_has_total_tokens_field(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertTrue(hasattr(AuroraPoolMetrics, "total_tokens"))

    def test_has_pool_utilization_field(self):
        from odoo.addons.aurora.models.pool_metrics import AuroraPoolMetrics
        self.assertTrue(hasattr(AuroraPoolMetrics, "pool_utilization"))


class TestPreviewWizard(unittest.TestCase):
    """Tests for preview_wizard.py _build_preview"""

    def test_max_preview_lines_constant(self):
        from odoo.addons.aurora.models.preview_wizard import _MAX_PREVIEW_LINES
        self.assertEqual(_MAX_PREVIEW_LINES, 50)

    def test_max_chars_per_line_constant(self):
        from odoo.addons.aurora.models.preview_wizard import _MAX_CHARS_PER_LINE
        self.assertEqual(_MAX_CHARS_PER_LINE, 600)

    def test_build_preview_valid_jsonl(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"key": "value"}\n')
            f.write('{"key2": "value2"}\n')
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 2)
            self.assertIn("key", text)
            self.assertEqual(count, 2)
        finally:
            os.unlink(path)

    def test_build_preview_empty_file(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 0)
            self.assertEqual(count, 0)
        finally:
            os.unlink(path)

    def test_build_preview_truncates_long_lines(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        long_value = "x" * 2000
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"data": long_value}) + "\n")
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 1)
            self.assertIn("truncated", text)
        finally:
            os.unlink(path)

    def test_build_preview_invalid_json_line(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("not json at all\n")
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 1)
            self.assertIn("not json", text)
            self.assertEqual(count, 1)
        finally:
            os.unlink(path)

    def test_build_preview_many_lines_truncated(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for i in range(100):
                f.write(json.dumps({"i": i}) + "\n")
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 100)
            self.assertIn("more records", text)
            self.assertEqual(count, 50)
        finally:
            os.unlink(path)

    def test_build_preview_separator(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a": 1}\n{"b": 2}\n')
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 2)
            self.assertIn("---", text)
        finally:
            os.unlink(path)

    def test_build_preview_single_line(self):
        from odoo.addons.aurora.models.preview_wizard import AuroraPipelinePreview
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"only": "one"}\n')
            path = f.name
        try:
            text, count = AuroraPipelinePreview._build_preview(path, 1)
            self.assertEqual(count, 1)
            self.assertNotIn("---", text)
        finally:
            os.unlink(path)


class TestEvaluationInstance(unittest.TestCase):
    """Tests for evaluation_instance.py"""

    def test_eval_instance_status_pending(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        keys = [s[0] for s in EVAL_INSTANCE_STATUS]
        self.assertIn("pending", keys)

    def test_eval_instance_status_building(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        keys = [s[0] for s in EVAL_INSTANCE_STATUS]
        self.assertIn("building", keys)

    def test_eval_instance_status_running(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        keys = [s[0] for s in EVAL_INSTANCE_STATUS]
        self.assertIn("running", keys)

    def test_eval_instance_status_resolved(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        keys = [s[0] for s in EVAL_INSTANCE_STATUS]
        self.assertIn("resolved", keys)

    def test_eval_instance_status_error(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        keys = [s[0] for s in EVAL_INSTANCE_STATUS]
        self.assertIn("error", keys)

    def test_eval_instance_status_count(self):
        from odoo.addons.aurora.models.evaluation_instance import EVAL_INSTANCE_STATUS
        self.assertEqual(len(EVAL_INSTANCE_STATUS), 7)

    def test_model_name(self):
        from odoo.addons.aurora.models.evaluation_instance import AuroraEvaluationInstance
        self.assertEqual(AuroraEvaluationInstance._name, "aurora.evaluation.instance")

    def test_model_order(self):
        from odoo.addons.aurora.models.evaluation_instance import AuroraEvaluationInstance
        self.assertEqual(AuroraEvaluationInstance._order, "evaluation_id desc, instance_id asc")

    def test_has_compute_display_name(self):
        from odoo.addons.aurora.models.evaluation_instance import AuroraEvaluationInstance
        self.assertTrue(hasattr(AuroraEvaluationInstance, "_compute_display_name"))

    def test_sql_constraints_exist(self):
        from odoo.addons.aurora.models.evaluation_instance import AuroraEvaluationInstance
        self.assertTrue(len(AuroraEvaluationInstance._sql_constraints) > 0)


class TestPipelineConfig(unittest.TestCase):
    """Tests for pipeline_config.py"""

    def test_github_lang_map_python(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["Python"], "python")

    def test_github_lang_map_javascript(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["JavaScript"], "javascript")

    def test_github_lang_map_typescript(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["TypeScript"], "typescript")

    def test_github_lang_map_go(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["Go"], "golang")

    def test_github_lang_map_cpp(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["C++"], "cpp")

    def test_github_lang_map_csharp(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["C#"], "csharp")

    def test_github_lang_map_java(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["Java"], "java")

    def test_github_lang_map_rust(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["Rust"], "rust")

    def test_github_lang_map_ruby(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["Ruby"], "ruby")

    def test_github_lang_map_c(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertEqual(GITHUB_LANG_MAP["C"], "c")

    def test_github_lang_map_case_sensitive_key(self):
        from odoo.addons.aurora.models.pipeline_config import GITHUB_LANG_MAP
        self.assertNotIn("python", GITHUB_LANG_MAP)

    def test_language_selection_contains_python(self):
        from odoo.addons.aurora.models.pipeline_config import LANGUAGE_SELECTION
        keys = [s[0] for s in LANGUAGE_SELECTION]
        self.assertIn("python", keys)

    def test_language_selection_contains_html(self):
        from odoo.addons.aurora.models.pipeline_config import LANGUAGE_SELECTION
        keys = [s[0] for s in LANGUAGE_SELECTION]
        self.assertIn("html", keys)

    def test_language_selection_count(self):
        from odoo.addons.aurora.models.pipeline_config import LANGUAGE_SELECTION
        self.assertEqual(len(LANGUAGE_SELECTION), 15)

    def test_lang_detection_mode_has_manual(self):
        from odoo.addons.aurora.models.pipeline_config import LANG_DETECTION_MODE
        keys = [s[0] for s in LANG_DETECTION_MODE]
        self.assertIn("manual", keys)

    def test_lang_detection_mode_has_automatic(self):
        from odoo.addons.aurora.models.pipeline_config import LANG_DETECTION_MODE
        keys = [s[0] for s in LANG_DETECTION_MODE]
        self.assertIn("automatic", keys)


class TestImportTokensWizardEdgeCases(unittest.TestCase):
    """Edge case tests for import_tokens_wizard.py not in existing test file"""

    def test_valid_prefixes_tuple(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _VALID_PREFIXES
        self.assertIsInstance(_VALID_PREFIXES, tuple)

    def test_valid_prefixes_count(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _VALID_PREFIXES
        self.assertEqual(len(_VALID_PREFIXES), 3)

    def test_min_token_length(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _MIN_TOKEN_LENGTH
        self.assertEqual(_MIN_TOKEN_LENGTH, 30)

    def test_token_hash_deterministic(self):
        token = "ghp_testtoken123456789012345678"
        h1 = hashlib.sha256(token.encode()).hexdigest()
        h2 = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(h1, h2)

    def test_token_shorter_than_min_rejected(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _VALID_PREFIXES, _MIN_TOKEN_LENGTH
        short_token = "ghp_short"
        has_prefix = any(short_token.startswith(p) for p in _VALID_PREFIXES)
        is_long_enough = len(short_token) >= _MIN_TOKEN_LENGTH
        self.assertTrue(has_prefix)
        self.assertFalse(is_long_enough)

    def test_token_with_valid_prefix_and_length_accepted(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _VALID_PREFIXES, _MIN_TOKEN_LENGTH
        valid_token = "ghp_" + "a" * 40
        has_prefix = any(valid_token.startswith(p) for p in _VALID_PREFIXES)
        is_long_enough = len(valid_token) >= _MIN_TOKEN_LENGTH
        self.assertTrue(has_prefix)
        self.assertTrue(is_long_enough)

    def test_github_pat_prefix_longer(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _VALID_PREFIXES, _MIN_TOKEN_LENGTH
        token = "github_pat_" + "b" * 30
        has_prefix = any(token.startswith(p) for p in _VALID_PREFIXES)
        is_long_enough = len(token) >= _MIN_TOKEN_LENGTH
        self.assertTrue(has_prefix)
        self.assertTrue(is_long_enough)

    def test_parse_csv_with_bom_marker(self):
        from odoo.addons.aurora.models.import_tokens_wizard import AuroraImportTokensWizard
        raw = "\ufeff".encode("utf-8-sig") + b"ghp_bomtest1234567890123456\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertTrue(len(result) >= 1)

    def test_parse_csv_multiple_columns_uses_first(self):
        from odoo.addons.aurora.models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"ghp_col1_abcdefghij,extra_col,another\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(len(result), 1)
        self.assertNotIn(",", result[0])

    def test_batch_size_positive(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _BATCH_SIZE
        self.assertGreater(_BATCH_SIZE, 0)

    def test_header_names_set_type(self):
        from odoo.addons.aurora.models.import_tokens_wizard import _HEADER_NAMES
        self.assertIsInstance(_HEADER_NAMES, set)


class TestFileViewer(unittest.TestCase):
    """Tests for controllers/file_viewer.py"""

    def test_is_path_under_base_valid(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/tmp/aurora_output/test.log", "/tmp/aurora_output")
        self.assertTrue(result)

    def test_is_path_under_base_invalid(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/etc/passwd", "/tmp/aurora_output")
        self.assertFalse(result)

    def test_is_path_under_base_empty_candidate(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("", "/tmp/aurora_output")
        self.assertFalse(result)

    def test_is_path_under_base_empty_base(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/tmp/test", "")
        self.assertFalse(result)

    def test_is_path_under_base_none_candidate(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base(None, "/tmp/aurora_output")
        self.assertFalse(result)

    def test_is_path_under_base_none_base(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/tmp/test", None)
        self.assertFalse(result)

    def test_is_path_under_base_exact_match(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/tmp/aurora_output", "/tmp/aurora_output")
        self.assertTrue(result)

    def test_is_path_under_base_traversal_blocked(self):
        from odoo.addons.aurora.controllers.file_viewer import _is_path_under_base
        result = _is_path_under_base("/tmp/aurora_output/../secret", "/tmp/aurora_output")
        self.assertFalse(result)

    def test_log_kinds_has_build_log(self):
        from odoo.addons.aurora.controllers.file_viewer import _LOG_KINDS
        self.assertIn("build_log", _LOG_KINDS)

    def test_log_kinds_has_run_log(self):
        from odoo.addons.aurora.controllers.file_viewer import _LOG_KINDS
        self.assertIn("run_log", _LOG_KINDS)

    def test_log_kinds_has_test_patch_log(self):
        from odoo.addons.aurora.controllers.file_viewer import _LOG_KINDS
        self.assertIn("test_patch_log", _LOG_KINDS)

    def test_log_kinds_has_fix_patch_log(self):
        from odoo.addons.aurora.controllers.file_viewer import _LOG_KINDS
        self.assertIn("fix_patch_log", _LOG_KINDS)

    def test_max_tail_bytes_value(self):
        from odoo.addons.aurora.controllers.file_viewer import _MAX_TAIL_BYTES
        self.assertEqual(_MAX_TAIL_BYTES, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
