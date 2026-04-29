# -*- coding: utf-8 -*-
import logging
from dataclasses import dataclass
from unittest import TestCase
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path


class TestConstants(TestCase):

    def test_build_image_workdir(self):
        from odoo.addons.aurora.tools.harness.constant import BUILD_IMAGE_WORKDIR
        self.assertEqual(BUILD_IMAGE_WORKDIR, "images")

    def test_instance_workdir(self):
        from odoo.addons.aurora.tools.harness.constant import INSTANCE_WORKDIR
        self.assertEqual(INSTANCE_WORKDIR, "instances")

    def test_evaluation_workdir(self):
        from odoo.addons.aurora.tools.harness.constant import EVALUATION_WORKDIR
        self.assertEqual(EVALUATION_WORKDIR, "evals")

    def test_report_file(self):
        from odoo.addons.aurora.tools.harness.constant import REPORT_FILE
        self.assertEqual(REPORT_FILE, "report.json")

    def test_final_report_file(self):
        from odoo.addons.aurora.tools.harness.constant import FINAL_REPORT_FILE
        self.assertEqual(FINAL_REPORT_FILE, "final_report.json")

    def test_run_log_file(self):
        from odoo.addons.aurora.tools.harness.constant import RUN_LOG_FILE
        self.assertEqual(RUN_LOG_FILE, "run.log")

    def test_test_patch_run_log(self):
        from odoo.addons.aurora.tools.harness.constant import TEST_PATCH_RUN_LOG_FILE
        self.assertEqual(TEST_PATCH_RUN_LOG_FILE, "test-patch-run.log")

    def test_fix_patch_run_log(self):
        from odoo.addons.aurora.tools.harness.constant import FIX_PATCH_RUN_LOG_FILE
        self.assertEqual(FIX_PATCH_RUN_LOG_FILE, "fix-patch-run.log")

    def test_all_log_files_are_strings(self):
        from odoo.addons.aurora.tools.harness import constant
        for attr in ["BUILD_IMAGE_LOG_FILE", "RUN_INSTANCE_LOG_FILE",
                      "RUN_EVALUATION_LOG_FILE", "GENERATE_REPORT_LOG_FILE",
                      "BUILD_DATASET_LOG_FILE"]:
            self.assertIsInstance(getattr(constant, attr), str)


class TestTestStatus(TestCase):

    def test_pass_value(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        self.assertEqual(TestStatus.PASS.value, "PASS")

    def test_fail_value(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        self.assertEqual(TestStatus.FAIL.value, "FAIL")

    def test_skip_value(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        self.assertEqual(TestStatus.SKIP.value, "SKIP")

    def test_none_value(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        self.assertEqual(TestStatus.NONE.value, "NONE")

    def test_all_statuses(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        names = {s.name for s in TestStatus}
        self.assertIn("PASS", names)
        self.assertIn("FAIL", names)
        self.assertIn("SKIP", names)
        self.assertIn("NONE", names)
        self.assertIn("PASSED", names)
        self.assertIn("FAILED", names)
        self.assertIn("SKIPPED", names)
        self.assertIn("ERROR", names)
        self.assertIn("XFAIL", names)


class TestTestResult(TestCase):

    def _make_result(self, passed=None, failed=None, skipped=None):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        passed = passed or set()
        failed = failed or set()
        skipped = skipped or set()
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

    def test_create_basic(self):
        r = self._make_result({"test_a"}, {"test_b"}, {"test_c"})
        self.assertEqual(r.passed_count, 1)
        self.assertEqual(r.failed_count, 1)
        self.assertEqual(r.skipped_count, 1)

    def test_all_count(self):
        r = self._make_result({"a", "b"}, {"c"}, {"d"})
        self.assertEqual(r.all_count, 4)

    def test_empty(self):
        r = self._make_result()
        self.assertEqual(r.all_count, 0)

    def test_mismatched_count_raises(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        with self.assertRaises(ValueError):
            TestResult(
                passed_count=999, failed_count=0, skipped_count=0,
                passed_tests=set(), failed_tests=set(), skipped_tests=set(),
            )

    def test_overlap_passed_failed_raises(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        with self.assertRaises(ValueError):
            TestResult(
                passed_count=1, failed_count=1, skipped_count=0,
                passed_tests={"test_a"}, failed_tests={"test_a"}, skipped_tests=set(),
            )

    def test_overlap_passed_skipped_raises(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        with self.assertRaises(ValueError):
            TestResult(
                passed_count=1, failed_count=0, skipped_count=1,
                passed_tests={"x"}, failed_tests=set(), skipped_tests={"x"},
            )

    def test_overlap_failed_skipped_raises(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        with self.assertRaises(ValueError):
            TestResult(
                passed_count=0, failed_count=1, skipped_count=1,
                passed_tests=set(), failed_tests={"x"}, skipped_tests={"x"},
            )

    def test_non_set_raises(self):
        from odoo.addons.aurora.tools.harness.test_result import TestResult
        with self.assertRaises(ValueError):
            TestResult(
                passed_count=0, failed_count=0, skipped_count=0,
                passed_tests=[], failed_tests=set(), skipped_tests=set(),
            )

    def test_internal_tests_dict(self):
        from odoo.addons.aurora.tools.harness.test_result import TestStatus
        r = self._make_result({"p"}, {"f"}, {"s"})
        self.assertEqual(r._tests["p"], TestStatus.PASS)
        self.assertEqual(r._tests["f"], TestStatus.FAIL)
        self.assertEqual(r._tests["s"], TestStatus.SKIP)


class TestMappingToTestResult(TestCase):

    def test_basic_mapping(self):
        from odoo.addons.aurora.tools.harness.test_result import mapping_to_testresult
        m = {"t1": "PASSED", "t2": "FAILED", "t3": "SKIPPED"}
        r = mapping_to_testresult(m)
        self.assertIn("t1", r.passed_tests)
        self.assertIn("t2", r.failed_tests)
        self.assertIn("t3", r.skipped_tests)

    def test_xfail_is_passed(self):
        from odoo.addons.aurora.tools.harness.test_result import mapping_to_testresult
        r = mapping_to_testresult({"t1": "XFAIL"})
        self.assertIn("t1", r.passed_tests)

    def test_error_is_failed(self):
        from odoo.addons.aurora.tools.harness.test_result import mapping_to_testresult
        r = mapping_to_testresult({"t1": "ERROR"})
        self.assertIn("t1", r.failed_tests)

    def test_empty(self):
        from odoo.addons.aurora.tools.harness.test_result import mapping_to_testresult
        r = mapping_to_testresult({})
        self.assertEqual(r.all_count, 0)


class TestGetModifiedFiles(TestCase):

    def test_basic_patch(self):
        from odoo.addons.aurora.tools.harness.test_result import get_modified_files
        diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = get_modified_files(diff)
        self.assertIn("src/main.py", result)

    def test_empty_patch(self):
        from odoo.addons.aurora.tools.harness.test_result import get_modified_files
        self.assertEqual(get_modified_files(""), [])

    def test_new_file_excluded(self):
        from odoo.addons.aurora.tools.harness.test_result import get_modified_files
        diff = "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n"
        result = get_modified_files(diff)
        self.assertEqual(result, [])


class TestRepository(TestCase):

    def test_create(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r = Repository(org="myorg", repo="myrepo")
        self.assertEqual(r.org, "myorg")
        self.assertEqual(r.repo, "myrepo")

    def test_repo_full_name(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r = Repository(org="o", repo="r")
        self.assertEqual(r.repo_full_name, "o/r")

    def test_repo_file_name(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r = Repository(org="o", repo="r")
        self.assertEqual(r.repo_file_name, "o__r")

    def test_repr(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r = Repository(org="o", repo="r")
        self.assertEqual(repr(r), "o/r")

    def test_hash(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r1 = Repository(org="o", repo="r")
        r2 = Repository(org="o", repo="r")
        self.assertEqual(hash(r1), hash(r2))

    def test_eq(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r1 = Repository(org="o", repo="r")
        r2 = Repository(org="o", repo="r")
        self.assertEqual(r1, r2)

    def test_neq(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r1 = Repository(org="o", repo="r1")
        r2 = Repository(org="o", repo="r2")
        self.assertNotEqual(r1, r2)

    def test_lt(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        r1 = Repository(org="a", repo="r")
        r2 = Repository(org="b", repo="r")
        self.assertLess(r1, r2)

    def test_invalid_org_raises(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        with self.assertRaises(ValueError):
            Repository(org=123, repo="r")

    def test_invalid_repo_raises(self):
        from odoo.addons.aurora.tools.harness.pull_request import Repository
        with self.assertRaises(ValueError):
            Repository(org="o", repo=123)


class TestPullRequestBase(TestCase):

    def test_create(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        pr = PullRequestBase(org="o", repo="r", number=42)
        self.assertEqual(pr.number, 42)

    def test_id(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        pr = PullRequestBase(org="o", repo="r", number=42)
        self.assertEqual(pr.id, "o/r:pr-42")

    def test_repr(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        pr = PullRequestBase(org="o", repo="r", number=42)
        self.assertEqual(repr(pr), "o/r:pr-42")

    def test_lt(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        pr1 = PullRequestBase(org="o", repo="r", number=1)
        pr2 = PullRequestBase(org="o", repo="r", number=2)
        self.assertLess(pr1, pr2)

    def test_invalid_number_raises(self):
        from odoo.addons.aurora.tools.harness.pull_request import PullRequestBase
        with self.assertRaises(ValueError):
            PullRequestBase(org="o", repo="r", number="not-int")


class TestResolvedIssue(TestCase):

    def test_create(self):
        from odoo.addons.aurora.tools.harness.pull_request import ResolvedIssue
        ri = ResolvedIssue(number=10, title="Bug", body="desc")
        self.assertEqual(ri.number, 10)

    def test_none_body(self):
        from odoo.addons.aurora.tools.harness.pull_request import ResolvedIssue
        ri = ResolvedIssue(number=10, title="Bug", body=None)
        self.assertIsNone(ri.body)

    def test_invalid_number(self):
        from odoo.addons.aurora.tools.harness.pull_request import ResolvedIssue
        with self.assertRaises(ValueError):
            ResolvedIssue(number="x", title="t", body="b")

    def test_invalid_title(self):
        from odoo.addons.aurora.tools.harness.pull_request import ResolvedIssue
        with self.assertRaises(ValueError):
            ResolvedIssue(number=1, title=123, body="b")


class TestBase(TestCase):

    def test_create(self):
        from odoo.addons.aurora.tools.harness.pull_request import Base
        b = Base(label="v1.0..v1.1", ref="main", sha="abc123")
        self.assertEqual(b.ref, "main")

    def test_invalid_label(self):
        from odoo.addons.aurora.tools.harness.pull_request import Base
        with self.assertRaises(ValueError):
            Base(label=123, ref="main", sha="abc")

    def test_invalid_ref(self):
        from odoo.addons.aurora.tools.harness.pull_request import Base
        with self.assertRaises(ValueError):
            Base(label="l", ref=123, sha="abc")

    def test_invalid_sha(self):
        from odoo.addons.aurora.tools.harness.pull_request import Base
        with self.assertRaises(ValueError):
            Base(label="l", ref="r", sha=123)


class TestInstance(TestCase):

    def test_registry_is_dict(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        self.assertIsInstance(Instance._registry, dict)

    def test_register_decorator(self):
        from odoo.addons.aurora.tools.harness.instance import Instance

        @Instance.register("test_org_xyz", "test_repo_xyz")
        class TestInst(Instance):
            pass

        self.assertIn("test_org_xyz/test_repo_xyz", Instance._registry)
        del Instance._registry["test_org_xyz/test_repo_xyz"]

    def test_create_unregistered_raises(self):
        from odoo.addons.aurora.tools.harness.instance import Instance
        from odoo.addons.aurora.tools.harness.pull_request import PullRequest, Base
        pr = PullRequest(
            org="unregistered_org", repo="unregistered_repo", number=1,
            state="closed", title="t", body="b",
            base=Base(label="", ref="main", sha="abc"),
            resolved_issues=[], fix_patch="", test_patch="",
        )
        with self.assertRaises(ValueError):
            Instance.create(pr, MagicMock())


class TestGitUtil(TestCase):

    @patch("odoo.addons.aurora.tools.harness.git_util.Path")
    def test_exists_false_no_path(self, MockPath):
        from odoo.addons.aurora.tools.harness.git_util import exists
        p = MagicMock()
        p.exists.return_value = False
        self.assertFalse(exists(p))

    @patch("subprocess.run")
    def test_is_clean_not_exists(self, mock_run):
        from odoo.addons.aurora.tools.harness.git_util import is_clean
        p = MagicMock()
        p.exists.return_value = False
        clean, msg = is_clean(p)
        self.assertFalse(clean)

    @patch("subprocess.run")
    def test_clean_calls_git(self, mock_run):
        from odoo.addons.aurora.tools.harness.git_util import clean
        clean("/tmp/repo")
        self.assertEqual(mock_run.call_count, 2)


class TestPythonTest(TestCase):

    def test_default_base_cmd(self):
        from odoo.addons.aurora.tools.harness.python_test import _DEFAULT_BASE_CMD
        self.assertIn("pytest", _DEFAULT_BASE_CMD)

    @patch("odoo.addons.aurora.tools.harness.python_test.get_modified_files", return_value=["tests/test_foo.py"])
    def test_command_with_files(self, mock_files):
        from odoo.addons.aurora.tools.harness.python_test import python_test_command
        cmd = python_test_command("fake patch")
        self.assertIn("tests/test_foo.py", cmd)

    @patch("odoo.addons.aurora.tools.harness.python_test.get_modified_files", return_value=[])
    def test_command_no_files(self, mock_files):
        from odoo.addons.aurora.tools.harness.python_test import python_test_command
        cmd = python_test_command("fake patch")
        self.assertIn("pytest", cmd)

    @patch("odoo.addons.aurora.tools.harness.python_test.get_modified_files", return_value=["test.py", "doc.md"])
    def test_only_py_filters(self, mock_files):
        from odoo.addons.aurora.tools.harness.python_test import python_test_command_only_py
        cmd = python_test_command_only_py("fake")
        self.assertIn("test.py", cmd)
        self.assertNotIn("doc.md", cmd)
