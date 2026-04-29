# -*- coding: utf-8 -*-
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock


class TestExtractPrNumbers(TestCase):

    def test_merge_pull_request(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("Merge pull request #123 from user/branch")
        self.assertIn(123, result)

    def test_parenthesized_number(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("feat: add feature (#456)")
        self.assertIn(456, result)

    def test_pr_hash(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("PR #789")
        self.assertIn(789, result)

    def test_no_match(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("just a regular commit message")
        self.assertEqual(result, [])

    def test_multiple_numbers(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("Merge pull request #10 (#20)")
        self.assertIn(10, result)
        self.assertIn(20, result)

    def test_dedup(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("Merge pull request #5 PR #5")
        self.assertEqual(result.count(5), 1)


class TestParseDate(TestCase):

    def test_iso_format(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        result = _parse_date("2024-01-15T10:30:00+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)

    def test_z_suffix(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        result = _parse_date("2024-01-15T10:30:00Z")
        self.assertIsNotNone(result)

    def test_empty_string(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        self.assertIsNone(_parse_date(""))

    def test_none(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        self.assertIsNone(_parse_date(None))

    def test_invalid(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        self.assertIsNone(_parse_date("not-a-date"))


class TestFilterPreReleases(TestCase):

    def test_filters_prereleases(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _filter_pre_releases
        tags = [
            {"is_pre_release": False, "name": "v1.0"},
            {"is_pre_release": True, "name": "v1.1-rc"},
            {"is_pre_release": False, "name": "v2.0"},
        ]
        result = _filter_pre_releases(tags)
        self.assertEqual(len(result), 2)

    def test_all_prereleases_kept(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _filter_pre_releases
        tags = [
            {"is_pre_release": True, "name": "v1.0-alpha"},
            {"is_pre_release": True, "name": "v1.0-beta"},
        ]
        result = _filter_pre_releases(tags)
        self.assertEqual(len(result), 2)

    def test_empty_list(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _filter_pre_releases
        self.assertEqual(_filter_pre_releases([]), [])


class TestGroupTagsByReleaseLine(TestCase):

    def test_groups_by_line(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_tags_by_release_line
        tags = [
            {"release_line": "1.0", "sort_key": (0, 1, 0, 0, (1, ""))},
            {"release_line": "1.0", "sort_key": (0, 1, 0, 1, (1, ""))},
            {"release_line": "2.0", "sort_key": (0, 2, 0, 0, (1, ""))},
        ]
        result = _group_tags_by_release_line(tags)
        self.assertEqual(len(result["1.0"]), 2)
        self.assertEqual(len(result["2.0"]), 1)

    def test_empty(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_tags_by_release_line
        self.assertEqual(_group_tags_by_release_line([]), {})

    def test_sorted_within_group(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_tags_by_release_line
        tags = [
            {"release_line": "1.0", "sort_key": (0, 1, 0, 2, (1, ""))},
            {"release_line": "1.0", "sort_key": (0, 1, 0, 0, (1, ""))},
            {"release_line": "1.0", "sort_key": (0, 1, 0, 1, (1, ""))},
        ]
        result = _group_tags_by_release_line(tags)
        sort_keys = [t["sort_key"] for t in result["1.0"]]
        self.assertEqual(sort_keys, sorted(sort_keys))


class TestIsAncestor(TestCase):

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_ancestor_returns_true(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _is_ancestor
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(_is_ancestor(Path("/repo"), "abc", "def"))

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_not_ancestor_returns_false(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _is_ancestor
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(_is_ancestor(Path("/repo"), "abc", "def"))

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_timeout_returns_false(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _is_ancestor
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=120)
        self.assertFalse(_is_ancestor(Path("/repo"), "abc", "def"))


class TestGetMergeCommits(TestCase):

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_parses_merge_commits(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_merge_commits
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123\n2024-01-01T00:00:00\nMerge pull request #42 from user/branch\n---END---\n"
        )
        result = _get_merge_commits(Path("/repo"), "base", "head")
        self.assertEqual(len(result), 1)
        self.assertIn(42, result[0]["pr_numbers"])

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_empty_output(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_merge_commits
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = _get_merge_commits(Path("/repo"), "base", "head")
        self.assertEqual(result, [])

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_failure_returns_empty(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_merge_commits
        mock_run.return_value = MagicMock(returncode=1)
        result = _get_merge_commits(Path("/repo"), "base", "head")
        self.assertEqual(result, [])


class TestGroupByTimeWindow(TestCase):

    def test_empty_prs(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        self.assertEqual(_group_by_time_window([]), [])

    def test_groups_within_window(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "a"},
            {"number": 2, "merged_at": "2024-01-10T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "b"},
            {"number": 3, "merged_at": "2024-01-20T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "c"},
        ]
        result = _group_by_time_window(prs, window_days=30)
        self.assertGreater(len(result), 0)

    def test_min_prs_per_bundle(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "a"},
        ]
        result = _group_by_time_window(prs, window_days=30)
        self.assertEqual(len(result), 0)

    def test_no_merged_at(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [{"number": 1, "base": {"ref": "main"}}]
        result = _group_by_time_window(prs)
        self.assertEqual(len(result), 0)


class TestConstants(TestCase):

    def test_compare_commits_cap(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _COMPARE_COMMITS_CAP
        self.assertEqual(_COMPARE_COMMITS_CAP, 250)

    def test_git_timeout(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _GIT_TIMEOUT
        self.assertEqual(_GIT_TIMEOUT, 120)

    def test_min_prs_per_bundle(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _MIN_PRS_PER_BUNDLE
        self.assertEqual(_MIN_PRS_PER_BUNDLE, 2)

    def test_pr_number_patterns_count(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _PR_NUMBER_PATTERNS
        self.assertEqual(len(_PR_NUMBER_PATTERNS), 4)

    def test_pr_number_patterns_are_compiled(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _PR_NUMBER_PATTERNS
        for p in _PR_NUMBER_PATTERNS:
            self.assertIsInstance(p, re.Pattern)
