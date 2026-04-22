import json
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase


class TestDatasetConverter(TransactionCase):
    """Test MetaSchemaConverter."""

    def _make_mock_instance(self, **overrides):
        """Create a mock instance record for converter testing."""
        defaults = {
            "name": "testorg__testrepo-42",
            "instance_id": "testorg__testrepo-42",
            "org": "testorg",
            "repo": "testrepo",
            "pr_number": 42,
            "base_sha": "abc123def",
            "language": "python",
            "tag": "v1.0.0",
            "title": "Fix bug in parser",
            "body": "This PR fixes the parser bug described in #10.",
            "fix_patch": "--- a/parser.py\n+++ b/parser.py\n@@ -1 +1 @@\n-old\n+new",
            "test_patch": "--- a/test_parser.py\n+++ b/test_parser.py\n@@ -1 +1 @@\n-old\n+new",
            "hints": "",
            "f2p_tests_json": json.dumps({"test_parser::test_bug": {"test": "FAIL", "fix": "PASS"}}),
            "p2p_tests_json": json.dumps({"test_parser::test_ok": {"test": "PASS", "fix": "PASS"}}),
            "docker_image_name": "mswebench/testorg_m_testrepo:pr-42",
            "dockerfile_content": "FROM python:3.11\nWORKDIR /testbed\nRUN pip install pytest\nCOPY . .\nCMD pytest",
            "docker_build_log": "Build successful",
            "conversation_log": "",
            "fix_patch_run_log": "pytest test_parser.py",
            "resolved_issues_json": json.dumps([{
                "number": 10,
                "title": "Parser fails on empty input",
                "body": "When passing empty string, parser crashes.",
            }]),
            "resolved_issue_ids": [],
        }
        defaults.update(overrides)

        mock = MagicMock()
        for key, val in defaults.items():
            setattr(mock, key, val)
        mock.repository_id = MagicMock()
        mock.repository_id.repo_url = "https://github.com/testorg/testrepo"
        return mock

    def test_convert_basic(self):
        """Test basic conversion produces all 26 fields."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter(
            ecr_prefix="123456789.dkr.ecr.us-east-1.amazonaws.com",
            task_category="hard_swe",
            repo_category="python_swe",
        )
        inst = self._make_mock_instance()
        result = converter.convert(inst)

        # Verify all 26 fields are present
        expected_keys = {
            "instance_id", "repo", "repo_path_or_url", "base_commit",
            "version", "language", "problem_statement", "functional_patch",
            "test_patch", "hints", "FAIL_TO_PASS", "PASS_TO_PASS",
            "docker_image_url", "docker_file", "container_mem",
            "container_memswap", "container_network_needed",
            "parsing_script", "run_script", "entrypoint_script",
            "before_repo_set_cmd", "selected_test_files_to_run",
            "task_category", "repo_category", "artifacts",
            "problem_statement_variants",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_convert_f2p_tests(self):
        """Test FAIL_TO_PASS extraction."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        inst = self._make_mock_instance()
        result = converter.convert(inst)
        self.assertIn("test_parser::test_bug", result["FAIL_TO_PASS"])

    def test_convert_docker_image_url(self):
        """Test ECR prefix is prepended to image URL."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter(ecr_prefix="123456.ecr.aws")
        inst = self._make_mock_instance()
        result = converter.convert(inst)
        self.assertTrue(result["docker_image_url"].startswith("123456.ecr.aws/"))

    def test_convert_missing_fix_patch_raises(self):
        """Test validation requires fix_patch."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        inst = self._make_mock_instance(fix_patch="")
        with self.assertRaises(ValueError):
            converter.convert(inst)

    def test_convert_batch(self):
        """Test batch conversion collects errors."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        good = self._make_mock_instance()
        bad = self._make_mock_instance(name="bad-1", fix_patch="", language="python")

        converted, errors = converter.convert_batch([good, bad])
        self.assertEqual(len(converted), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "bad-1")

    def test_python_parsing_script(self):
        """Test Python parsing script is selected for Python language."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        inst = self._make_mock_instance(language="python")
        result = converter.convert(inst)
        self.assertIn("pytest", result["parsing_script"])

    def test_version_from_tag(self):
        """Test version uses tag when present."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        inst = self._make_mock_instance(tag="v2.0.0")
        result = converter.convert(inst)
        self.assertEqual(result["version"], "v2.0.0")

    def test_version_falls_back_to_sha(self):
        """Test version falls back to base_sha when no tag."""
        from odoo.addons.jaeger.tools.dataset_converter import MetaSchemaConverter

        converter = MetaSchemaConverter()
        inst = self._make_mock_instance(tag="")
        result = converter.convert(inst)
        self.assertEqual(result["version"], "abc123def")
