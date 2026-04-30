# -*- coding: utf-8 -*-
import json
import os
import re
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock


class TestSplitPatches(TestCase):

    def test_test_file_classified(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/tests/test_foo.py b/tests/test_foo.py\n--- a/tests/test_foo.py\n+++ b/tests/test_foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertTrue(len(test) > 0)
        self.assertEqual(fix, "")

    def test_src_file_classified(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertTrue(len(fix) > 0)
        self.assertEqual(test, "")

    def test_empty_diff(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        fix, test = split_patches("")
        self.assertEqual(fix, "")
        self.assertEqual(test, "")

    def test_malformed_diff_returns_raw(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        fix, test = split_patches("not a diff at all")
        self.assertEqual(test, "")

    def test_test_path_keywords(self):
        from odoo.addons.aurora.tools.collect.build_dataset import TEST_PATH_KEYWORDS
        self.assertIn("test", TEST_PATH_KEYWORDS)
        self.assertIn("tests", TEST_PATH_KEYWORDS)
        self.assertIn("spec", TEST_PATH_KEYWORDS)
        self.assertIn("__tests__", TEST_PATH_KEYWORDS)
        self.assertIn("e2e", TEST_PATH_KEYWORDS)


class TestExtractIssueNumbers(TestCase):

    def test_closes_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("closes #42")
        self.assertIn(42, result)

    def test_fixes_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixes #10")
        self.assertIn(10, result)

    def test_resolves_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("resolves #5")
        self.assertIn(5, result)

    def test_url_pattern(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("see https://github.com/org/repo/issues/99")
        self.assertIn(99, result)

    def test_empty_body(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        self.assertEqual(extract_issue_numbers_from_body(""), [])

    def test_none_body(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        self.assertEqual(extract_issue_numbers_from_body(None), [])

    def test_no_issues(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        self.assertEqual(extract_issue_numbers_from_body("no issues here"), [])

    def test_multiple_issues(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixes #1, closes #2, resolves #3")
        self.assertEqual(sorted(result), [1, 2, 3])

    def test_dedup(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixes #5 closes #5")
        self.assertEqual(result.count(5), 1)

    def test_sorted_output(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixes #30 fixes #10 fixes #20")
        self.assertEqual(result, sorted(result))


class TestAggregateIssues(TestCase):

    def test_basic_aggregation(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "fixes #10", "title": "Fix bug", "resolved_issues": [10]}]
        issues = {10: {"number": 10, "title": "Bug", "body": "Bug description"}}
        result = aggregate_issues(prs, issues)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 10)
        self.assertEqual(result[0]["body"], "Bug description")

    def test_pseudo_issue_when_no_links(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "no links", "title": "PR title", "resolved_issues": []}]
        result = aggregate_issues(prs, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 1)
        self.assertEqual(result[0]["title"], "PR title")

    def test_empty_issue_body_substituted(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "PR body text", "title": "PR", "resolved_issues": [10]}]
        issues = {10: {"number": 10, "title": "Bug", "body": ""}}
        result = aggregate_issues(prs, issues)
        self.assertEqual(result[0]["body"], "PR body text")

    def test_dedup_across_prs(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [
            {"number": 1, "body": "fixes #10", "title": "", "resolved_issues": [10]},
            {"number": 2, "body": "fixes #10", "title": "", "resolved_issues": [10]},
        ]
        issues = {10: {"number": 10, "title": "Bug", "body": "desc"}}
        result = aggregate_issues(prs, issues)
        nums = [r["number"] for r in result]
        self.assertEqual(nums.count(10), 1)

    def test_empty_prs(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        self.assertEqual(aggregate_issues([], {}), [])


class TestRepoCloneCache(TestCase):

    def test_repo_path(self):
        from odoo.addons.aurora.tools.collect.build_dataset import RepoCloneCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            path = cache._repo_path("myorg", "myrepo")
            self.assertEqual(path.name, "myorg__myrepo.git")

    @patch("subprocess.run")
    def test_get_diff_calls_git(self, mock_run):
        from odoo.addons.aurora.tools.collect.build_dataset import RepoCloneCache
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "org__repo.git"
            repo_path.mkdir()
            cache = RepoCloneCache(tmpdir)
            mock_run.return_value = MagicMock(returncode=0, stdout="diff output")
            result = cache.get_diff("org", "repo", "abc", "def")
            self.assertEqual(result, "diff output")

    def test_get_diff_empty_sha_raises(self):
        from odoo.addons.aurora.tools.collect.build_dataset import RepoCloneCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            with self.assertRaises(ValueError):
                cache.get_diff("org", "repo", "", "def")
            with self.assertRaises(ValueError):
                cache.get_diff("org", "repo", "abc", "")


class TestFetchUnifiedDiff(TestCase):

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    def test_success_from_api(self, mock_get):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_resp = MagicMock(status_code=200, text="diff content")
        mock_get.return_value = mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            result = fetch_unified_diff("org", "repo", "abc", "def", "ghp_token", cache)
        self.assertEqual(result, "diff content")

    def test_empty_sha_raises(self):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            with self.assertRaises(ValueError):
                fetch_unified_diff("org", "repo", "", "def", "tok", cache)


class TestIssuePatterns(TestCase):

    def test_issue_ref_pattern(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_REF_PATTERN
        self.assertIsInstance(ISSUE_REF_PATTERN, re.Pattern)
        self.assertTrue(ISSUE_REF_PATTERN.search("closes #42"))
        self.assertTrue(ISSUE_REF_PATTERN.search("fixes #10"))
        self.assertTrue(ISSUE_REF_PATTERN.search("resolves #5"))

    def test_issue_url_pattern(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_URL_PATTERN
        self.assertIsInstance(ISSUE_URL_PATTERN, re.Pattern)
        m = ISSUE_URL_PATTERN.search("https://github.com/org/repo/issues/99")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "99")
