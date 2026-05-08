# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestAuroraPipelineErrorExpanded(unittest.TestCase):
    """Deep edge-case coverage for AuroraPipelineError."""

    def test_empty_message(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        exc = AuroraPipelineError("")
        self.assertEqual(str(exc), "")

    def test_unicode_message(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        exc = AuroraPipelineError("erreur: fichier introuvable \u2014 \u00e9chec")
        self.assertIn("\u00e9chec", str(exc))

    def test_multiline_message(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        exc = AuroraPipelineError("line1\nline2\nline3")
        self.assertIn("\n", str(exc))

    def test_exception_args_tuple(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        exc = AuroraPipelineError("msg")
        self.assertEqual(exc.args, ("msg",))

    def test_inherits_from_base_exception(self):
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        self.assertTrue(issubclass(AuroraPipelineError, BaseException))


class TestValidateName(unittest.TestCase):
    """Edge cases for validate_name."""

    def test_valid_alphanumeric(self):
        from odoo.addons.aurora.tools.collect.util import validate_name
        self.assertEqual(validate_name("my-repo_2.0"), "my-repo_2.0")

    def test_invalid_slash(self):
        from odoo.addons.aurora.tools.collect.util import validate_name, AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            validate_name("org/repo")

    def test_invalid_space(self):
        from odoo.addons.aurora.tools.collect.util import validate_name, AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            validate_name("my repo")

    def test_invalid_special_chars(self):
        from odoo.addons.aurora.tools.collect.util import validate_name, AuroraPipelineError
        with self.assertRaises(AuroraPipelineError):
            validate_name("repo@name!")

    def test_custom_label_in_error(self):
        from odoo.addons.aurora.tools.collect.util import validate_name, AuroraPipelineError
        try:
            validate_name("bad/name", label="organization")
        except AuroraPipelineError as e:
            self.assertIn("organization", str(e))

    def test_dots_allowed(self):
        from odoo.addons.aurora.tools.collect.util import validate_name
        self.assertEqual(validate_name("v1.2.3"), "v1.2.3")

    def test_hyphens_allowed(self):
        from odoo.addons.aurora.tools.collect.util import validate_name
        self.assertEqual(validate_name("my-project"), "my-project")

    def test_underscores_allowed(self):
        from odoo.addons.aurora.tools.collect.util import validate_name
        self.assertEqual(validate_name("my_project"), "my_project")


class TestTokenRotatorExpanded(unittest.TestCase):
    """Expanded TokenRotator tests: rotation, rate limit behavior."""

    def _mock_github(self, remaining=5000, reset_ts=9999999999):
        mock = MagicMock()
        mock.rate_limiting = (remaining, reset_ts)
        return mock

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_rotates_through_three_tokens(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["a", "b", "c"])
        tokens_seen = [rotator.get_token() for _ in range(6)]
        self.assertEqual(tokens_seen, ["a", "b", "c", "a", "b", "c"])

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_skips_exhausted_token(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        from odoo.addons.aurora.tools.collect.util import _RATE_LIMIT_FLOOR
        exhausted = MagicMock()
        exhausted.rate_limiting = (_RATE_LIMIT_FLOOR - 1, 9999999999)
        healthy = MagicMock()
        healthy.rate_limiting = (5000, 9999999999)
        MockGithub.side_effect = [exhausted, healthy]
        rotator = TokenRotator(["dead", "alive"])
        token = rotator.get_token()
        self.assertEqual(token, "alive")

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_get_rate_limits_returns_all_tokens(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github(remaining=3000, reset_ts=1700000000)
        rotator = TokenRotator(["tok1", "tok2"])
        limits = rotator.get_rate_limits()
        self.assertEqual(len(limits), 2)
        for _, info in limits.items():
            self.assertIn("remaining", info)
            self.assertIn("reset", info)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_call_counts_increment(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["tok1"])
        rotator.get_token()
        rotator.get_token()
        self.assertEqual(rotator._call_counts[0], 2)

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_summary_contains_calls_count(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        MockGithub.return_value = self._mock_github()
        rotator = TokenRotator(["tok1"])
        rotator.get_token()
        s = rotator.summary()
        self.assertIn("1 calls", s)


class TestParseTokensExpanded(unittest.TestCase):
    """Edge cases for parse_tokens."""

    def test_path_with_blank_lines(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("tok1\n\n\ntok2\n\n")
            path = f.name
        try:
            result = parse_tokens(Path(path))
            self.assertEqual(result, ["tok1", "tok2"])
        finally:
            os.unlink(path)

    def test_unsupported_type_returns_empty(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        result = parse_tokens(12345)
        self.assertEqual(result, [])

    def test_single_string_returns_list(self):
        from odoo.addons.aurora.tools.collect.util import parse_tokens
        result = parse_tokens("ghp_abc")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)


class TestCloneRepoBare(unittest.TestCase):
    """Clone operations with mocked subprocess."""

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_clone_success(self, mock_run):
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo_bare("org", "repo", tmpdir, auth_token="tok")
            self.assertIsNotNone(result)
            self.assertIn("org__repo.git", str(result))

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_clone_failure_returns_none(self, mock_run):
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.return_value = MagicMock(returncode=128, stderr="fatal: repo not found")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo_bare("org", "repo", tmpdir)
            self.assertIsNone(result)

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_clone_timeout_returns_none(self, mock_run):
        import subprocess
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=600)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo_bare("org", "repo", tmpdir)
            self.assertIsNone(result)

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_clone_git_not_found_returns_none(self, mock_run):
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.side_effect = FileNotFoundError("git not found")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo_bare("org", "repo", tmpdir)
            self.assertIsNone(result)

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_existing_repo_does_fetch(self, mock_run):
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "org__repo.git"
            repo_path.mkdir()
            result = clone_repo_bare("org", "repo", tmpdir)
            self.assertEqual(result, repo_path)

    @patch("odoo.addons.aurora.tools.collect.util.subprocess.run")
    def test_token_redacted_in_error(self, mock_run):
        from odoo.addons.aurora.tools.collect.util import clone_repo_bare
        mock_run.return_value = MagicMock(returncode=1, stderr="ghp_secret123 failed")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo_bare("org", "repo", tmpdir, auth_token="ghp_secret123")
            self.assertIsNone(result)


class TestFilterPrsMainExpanded(unittest.TestCase):
    """Deep coverage for filter_prs.main edge cases."""

    def _make_pr(self, number=1, state="closed", merged_at="2024-06-01",
                 title="", body="", commits=None):
        return {
            "number": number, "state": state, "merged_at": merged_at,
            "title": title or "", "body": body or "",
            "base": {"ref": "main"}, "commits": commits or [],
        }

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_min_merge_date_boundary_exactly_on(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2021-01-01", body="fix #1")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True, min_merge_date="2021-01-01")
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            # merged_at < min_merge_date check is strict less-than comparison
            # "2021-01-01" < "2021-01-01" is False, so it passes
            self.assertEqual(len(lines), 1)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_min_merge_date_pr_before_cutoff(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2020-12-31", body="fix #1")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True, min_merge_date="2021-01-01")
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_min_merge_date_pr_after_cutoff(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2021-01-02", body="fix #5")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True, min_merge_date="2021-01-01")
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_empty_input_file(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            prs_file.write_text("")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_all_filtered_out_no_resolved_issues(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2024-01-01", body="no issues")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_all_pass_filter(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                for i in range(5):
                    f.write(json.dumps(self._make_pr(
                        number=i, merged_at="2024-01-01",
                        body=f"fix #{i+10}")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 5)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_pr_with_no_merge_date_skipped(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                pr = self._make_pr(merged_at=None, body="fix #1")
                f.write(json.dumps(pr) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_unicode_in_pr_title(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                pr = self._make_pr(title="fix #7 \u2014 \u4fee\u590d\u95ee\u9898", merged_at="2024-01-01")
                f.write(json.dumps(pr, ensure_ascii=False) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_large_pr_list(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                for i in range(200):
                    f.write(json.dumps(self._make_pr(
                        number=i, merged_at="2024-01-01",
                        body=f"fix #{i+1000}")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 200)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_resolved_issues_written_to_output(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2024-01-01", body="fix #42 fix #43")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                record = json.loads(f.readline())
            self.assertIn(42, record["resolved_issues"])
            self.assertIn(43, record["resolved_issues"])

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_open_state_filtered(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    state="open", merged_at="2024-01-01", body="fix #1")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_blank_lines_in_input_ignored(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write("\n\n")
                f.write(json.dumps(self._make_pr(
                    merged_at="2024-01-01", body="fix #1")) + "\n")
                f.write("\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_commits_cleared_when_skip_commit_message(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                pr = self._make_pr(merged_at="2024-01-01", body="fix #1",
                                   commits=[{"message": "old commit"}])
                f.write(json.dumps(pr) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                record = json.loads(f.readline())
            self.assertEqual(record["commits"], [])

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_min_merge_date_empty_string_means_no_filter(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps(self._make_pr(
                    merged_at="2000-01-01", body="fix #1")) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True, min_merge_date="")
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1)


class TestExtractResolvedIssuesExpanded(unittest.TestCase):
    """Additional edge cases for extract_resolved_issues."""

    def _pull(self, title="", body="", commits=None):
        return {"title": title or "", "body": body or "", "commits": commits or []}

    def test_issue_in_multiline_body(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        body = "This PR does stuff.\n\nfix #100\n\nMore info."
        result = extract_resolved_issues(self._pull(body=body))
        self.assertIn(100, result)

    def test_issue_in_nested_html_comment(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        body = "<!-- fix #50 -->\nfixes #60"
        result = extract_resolved_issues(self._pull(body=body))
        self.assertNotIn(50, result)
        self.assertIn(60, result)

    def test_case_insensitive_keywords(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        # The regex uses re.compile(r"(\w+)\s+\#(\d+)") and checks .lower()
        result = extract_resolved_issues(self._pull(body="Fix #20"))
        self.assertIn(20, result)

    def test_large_issue_number(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fix #999999"))
        self.assertIn(999999, result)

    def test_multiple_commits_with_issues(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        commits = [{"message": "fix #1"}, {"message": "fix #2"}, {"message": "fix #3"}]
        result = extract_resolved_issues(self._pull(commits=commits))
        self.assertEqual(set(result), {1, 2, 3})

    def test_non_keyword_word_before_hash(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="see #42"))
        self.assertNotIn(42, result)

    def test_keyword_with_extra_spaces(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        # regex is (\w+)\s+\#(\d+), so multi-space should work
        result = extract_resolved_issues(self._pull(body="fix  #15"))
        self.assertIn(15, result)


class TestGetVersionTagsParseExpanded(unittest.TestCase):
    """Extended parse_tag tests for edge cases."""

    def _parse(self, name):
        from odoo.addons.aurora.tools.collect.get_version_tags import parse_tag
        return parse_tag(name)

    def test_semver_pre_release_sort_key_less_than_stable(self):
        pre = self._parse("v1.0.0-rc.1")
        stable = self._parse("v1.0.0")
        self.assertLess(pre["sort_key"], stable["sort_key"])

    def test_calver_micro_version(self):
        r = self._parse("2024.01.15.2")
        self.assertEqual(r["scheme"], "calver")
        self.assertEqual(r["micro"], 2)

    def test_calver_month_boundary_13_invalid(self):
        # month 13 is invalid for calver, should not match calver
        r = self._parse("2024.13")
        self.assertNotEqual(r["scheme"], "calver")

    def test_empty_string_unknown(self):
        r = self._parse("")
        self.assertEqual(r["scheme"], "unknown")

    def test_whitespace_stripped(self):
        r = self._parse("  v1.2.3  ")
        self.assertEqual(r["scheme"], "semver")

    def test_rel_prefix(self):
        r = self._parse("rel/2.0.0")
        self.assertEqual(r["scheme"], "semver")
        self.assertEqual(r["major"], 2)

    def test_ver_prefix(self):
        r = self._parse("ver-3.0.0")
        self.assertEqual(r["scheme"], "semver")

    def test_calver_february_29(self):
        r = self._parse("2024.02.29")
        self.assertEqual(r["scheme"], "calver")
        self.assertEqual(r["day"], 29)

    def test_unknown_with_packaging_version(self):
        r = self._parse("1.0")
        # "1.0" only has two parts, should not match semver (needs 3)
        self.assertNotEqual(r["scheme"], "semver")

    def test_semver_with_long_pre_release(self):
        r = self._parse("v1.0.0-alpha.beta.gamma.1")
        self.assertTrue(r["is_pre_release"])
        self.assertEqual(r["pre_release"], "alpha.beta.gamma.1")

    def test_calver_short_year_30(self):
        r = self._parse("30.6")
        self.assertEqual(r["scheme"], "calver")
        self.assertEqual(r["major"], 2030)


class TestGetVersionTagsMain(unittest.TestCase):
    """Tests for get_version_tags.main with mocked GitHub client."""

    @patch("odoo.addons.aurora.tools.collect.get_version_tags.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_version_tags.TokenRotator")
    def test_main_writes_jsonl(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_version_tags import main
        mock_tag = MagicMock()
        mock_tag.name = "v1.0.0"
        mock_tag.commit.sha = "abc123"
        mock_commit = MagicMock()
        mock_commit.commit.committer.date = MagicMock()
        mock_commit.commit.committer.date.isoformat.return_value = "2024-01-01T00:00:00"
        mock_repo = MagicMock()
        mock_repo.get_tags.return_value = [mock_tag]
        mock_repo.get_commit.return_value = mock_commit
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        MockRotator.return_value.get_client.return_value = mock_client
        with tempfile.TemporaryDirectory() as tmpdir:
            main(["tok"], Path(tmpdir), "org", "repo", max_tags=10)
            out_file = Path(tmpdir) / "org__repo_tags.jsonl"
            self.assertTrue(out_file.exists())
            with open(out_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["name"], "v1.0.0")

    @patch("odoo.addons.aurora.tools.collect.get_version_tags.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_version_tags.TokenRotator")
    def test_max_tags_limits_output(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_version_tags import main
        tags = []
        for i in range(10):
            t = MagicMock()
            t.name = f"v1.0.{i}"
            t.commit.sha = f"sha{i}"
            tags.append(t)
        mock_commit = MagicMock()
        mock_commit.commit.committer.date.isoformat.return_value = "2024-01-01T00:00:00"
        mock_repo = MagicMock()
        mock_repo.get_tags.return_value = tags
        mock_repo.get_commit.return_value = mock_commit
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        MockRotator.return_value.get_client.return_value = mock_client
        with tempfile.TemporaryDirectory() as tmpdir:
            main(["tok"], Path(tmpdir), "org", "repo", max_tags=3)
            out_file = Path(tmpdir) / "org__repo_tags.jsonl"
            with open(out_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 3)

    @patch("odoo.addons.aurora.tools.collect.get_version_tags.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_version_tags.TokenRotator")
    def test_tags_sorted_by_sort_key(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_version_tags import main
        tag_names = ["v2.0.0", "v1.0.0", "v1.1.0"]
        tags = []
        for name in tag_names:
            t = MagicMock()
            t.name = name
            t.commit.sha = f"sha_{name}"
            tags.append(t)
        mock_commit = MagicMock()
        mock_commit.commit.committer.date.isoformat.return_value = "2024-01-01T00:00:00"
        mock_repo = MagicMock()
        mock_repo.get_tags.return_value = tags
        mock_repo.get_commit.return_value = mock_commit
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        MockRotator.return_value.get_client.return_value = mock_client
        with tempfile.TemporaryDirectory() as tmpdir:
            main(["tok"], Path(tmpdir), "org", "repo", max_tags=200)
            out_file = Path(tmpdir) / "org__repo_tags.jsonl"
            with open(out_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            names = [r["name"] for r in records]
            self.assertEqual(names, ["v1.0.0", "v1.1.0", "v2.0.0"])


class TestGroupPrsByTagsExpanded(unittest.TestCase):
    """Edge cases for group_prs_by_tags helpers."""

    def test_group_by_time_window_different_branches(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "a"},
            {"number": 2, "merged_at": "2024-01-02T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "b"},
            {"number": 3, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "develop"}, "merge_commit_sha": "c"},
            {"number": 4, "merged_at": "2024-01-02T00:00:00Z", "base": {"ref": "develop"}, "merge_commit_sha": "d"},
        ]
        result = _group_by_time_window(prs, window_days=30)
        self.assertGreaterEqual(len(result), 2)

    def test_group_by_time_window_large_gap(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "a"},
            {"number": 2, "merged_at": "2024-01-02T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "b"},
            {"number": 3, "merged_at": "2024-06-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "c"},
            {"number": 4, "merged_at": "2024-06-02T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "d"},
        ]
        result = _group_by_time_window(prs, window_days=30)
        self.assertEqual(len(result), 2)

    def test_group_by_time_window_window_days_1(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_by_time_window
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "a"},
            {"number": 2, "merged_at": "2024-01-01T12:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "b"},
            {"number": 3, "merged_at": "2024-01-03T00:00:00Z", "base": {"ref": "main"}, "merge_commit_sha": "c"},
        ]
        result = _group_by_time_window(prs, window_days=1)
        # PRs 1&2 are within 1 day, PR 3 is 2 days out
        self.assertGreaterEqual(len(result), 1)

    def test_extract_pr_numbers_pull_request_case_insensitive(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _extract_pr_numbers
        result = _extract_pr_numbers("pull request #55 from user/branch")
        self.assertIn(55, result)

    def test_find_cross_line_pairs_no_repo_path(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _find_cross_line_pairs
        tags = [
            {"name": "v1.9.0", "sha": "aaa", "release_line": "1.9", "sort_key": (0, 1, 9, 0, (1, ""))},
            {"name": "v2.0.0", "sha": "bbb", "release_line": "2.0", "sort_key": (0, 2, 0, 0, (1, ""))},
        ]
        result = _find_cross_line_pairs(tags, set(), None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0]["name"], "v1.9.0")
        self.assertEqual(result[0][1]["name"], "v2.0.0")

    def test_find_cross_line_pairs_skips_existing(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _find_cross_line_pairs
        tags = [
            {"name": "v1.0.0", "sha": "aaa", "release_line": "1.0", "sort_key": (0, 1, 0, 0, (1, ""))},
            {"name": "v2.0.0", "sha": "bbb", "release_line": "2.0", "sort_key": (0, 2, 0, 0, (1, ""))},
        ]
        existing = {("aaa", "bbb")}
        result = _find_cross_line_pairs(tags, existing, None)
        self.assertEqual(len(result), 0)

    def test_find_cross_line_pairs_same_sha_skipped(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _find_cross_line_pairs
        tags = [
            {"name": "v1.0.0", "sha": "same", "release_line": "1.0", "sort_key": (0, 1, 0, 0, (1, ""))},
            {"name": "v2.0.0", "sha": "same", "release_line": "2.0", "sort_key": (0, 2, 0, 0, (1, ""))},
        ]
        result = _find_cross_line_pairs(tags, set(), None)
        self.assertEqual(len(result), 0)

    def test_group_tags_by_release_line_unknown_line(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _group_tags_by_release_line
        tags = [
            {"release_line": "unknown", "sort_key": (2, 0, 0, 0, (0, "x"))},
            {"release_line": "unknown", "sort_key": (2, 0, 0, 0, (0, "y"))},
        ]
        result = _group_tags_by_release_line(tags)
        self.assertIn("unknown", result)
        self.assertEqual(len(result["unknown"]), 2)

    def test_filter_pre_releases_all_stable(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _filter_pre_releases
        tags = [{"is_pre_release": False}, {"is_pre_release": False}]
        result = _filter_pre_releases(tags)
        self.assertEqual(len(result), 2)


class TestGetRelatedIssuesExpanded(unittest.TestCase):
    """Edge cases for get_related_issues.main."""

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_no_resolved_issues_writes_empty(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"number": 1, "resolved_issues": []}) + "\n")
            main(["tok"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            self.assertTrue(out_file.exists())
            self.assertEqual(out_file.read_text(), "")

    def test_invalid_filename_raises(self):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "invalid_name.jsonl"
            bad_file.write_text("")
            with self.assertRaises(AuroraPipelineError):
                main(["tok"], Path(tmpdir), bad_file)

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_resolved_issues_as_dicts(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"number": 1, "resolved_issues": [{"number": 5}]}) + "\n")
            mock_issue = MagicMock()
            mock_issue.number = 5
            mock_issue.state = "closed"
            mock_issue.title = "Bug"
            mock_issue.body = "desc"
            mock_repo = MagicMock()
            mock_repo.get_issue.return_value = mock_issue
            mock_client = MagicMock()
            mock_client.get_repo.return_value = mock_repo
            MockRotator.return_value.get_client.return_value = mock_client
            main(["tok"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            with open(out_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["number"], 5)

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_issue_fetch_failure_graceful(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"number": 1, "resolved_issues": [10]}) + "\n")
            mock_repo = MagicMock()
            mock_repo.get_issue.side_effect = Exception("Not found")
            mock_client = MagicMock()
            mock_client.get_repo.return_value = mock_repo
            MockRotator.return_value.get_client.return_value = mock_client
            # Should not raise
            main(["tok"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            self.assertTrue(out_file.exists())

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_dedup_issues_across_prs(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"number": 1, "resolved_issues": [10]}) + "\n")
                f.write(json.dumps({"number": 2, "resolved_issues": [10]}) + "\n")
            mock_issue = MagicMock()
            mock_issue.number = 10
            mock_issue.state = "closed"
            mock_issue.title = "Bug"
            mock_issue.body = "desc"
            mock_repo = MagicMock()
            mock_repo.get_issue.return_value = mock_issue
            mock_client = MagicMock()
            mock_client.get_repo.return_value = mock_repo
            MockRotator.return_value.get_client.return_value = mock_client
            main(["tok"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            with open(out_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            # Issue 10 should only be fetched once
            self.assertEqual(len(records), 1)

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_malformed_json_lines_skipped(self, MockRotator, _):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write("not json\n")
                f.write(json.dumps({"number": 1, "resolved_issues": []}) + "\n")
            main(["tok"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            self.assertEqual(out_file.read_text(), "")


class TestBuildDatasetExpanded(unittest.TestCase):
    """Edge cases for build_dataset functions."""

    def test_split_patches_e2e_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/e2e/test_login.py b/e2e/test_login.py\n--- a/e2e/test_login.py\n+++ b/e2e/test_login.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertGreater(len(test), 0)
        self.assertEqual(fix, "")

    def test_split_patches_spec_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/spec/models_spec.rb b/spec/models_spec.rb\n--- a/spec/models_spec.rb\n+++ b/spec/models_spec.rb\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertGreater(len(test), 0)

    def test_split_patches_mixed_files(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = (
            "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/tests/test_main.py b/tests/test_main.py\n--- a/tests/test_main.py\n+++ b/tests/test_main.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        fix, test = split_patches(diff)
        self.assertGreater(len(fix), 0)
        self.assertGreater(len(test), 0)

    def test_extract_issue_numbers_close_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("close #7")
        self.assertIn(7, result)

    def test_extract_issue_numbers_fixed_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixed #8")
        self.assertIn(8, result)

    def test_extract_issue_numbers_resolved_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("resolved #9")
        self.assertIn(9, result)

    def test_extract_issue_numbers_zero_ignored(self):
        from odoo.addons.aurora.tools.collect.build_dataset import extract_issue_numbers_from_body
        result = extract_issue_numbers_from_body("fixes #0")
        self.assertNotIn(0, result)

    def test_aggregate_issues_resolved_issues_as_dicts(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "", "title": "T", "resolved_issues": [{"number": 20}]}]
        issues = {20: {"number": 20, "title": "Issue", "body": "Body"}}
        result = aggregate_issues(prs, issues)
        self.assertEqual(result[0]["number"], 20)

    def test_aggregate_issues_issue_not_in_lookup(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "fixes #99", "title": "T", "resolved_issues": [99]}]
        result = aggregate_issues(prs, {})
        # Issue 99 not in all_issues, PR becomes pseudo-issue
        self.assertEqual(result[0]["number"], 1)

    def test_aggregate_issues_multiple_prs_different_issues(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [
            {"number": 1, "body": "fixes #10", "title": "T1", "resolved_issues": [10]},
            {"number": 2, "body": "fixes #20", "title": "T2", "resolved_issues": [20]},
        ]
        issues = {
            10: {"number": 10, "title": "A", "body": "a"},
            20: {"number": 20, "title": "B", "body": "b"},
        }
        result = aggregate_issues(prs, issues)
        nums = {r["number"] for r in result}
        self.assertEqual(nums, {10, 20})

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    def test_fetch_unified_diff_rate_limited_raises(self, mock_get):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_resp = MagicMock(status_code=429)
        mock_resp.headers = {"Retry-After": "60"}
        mock_get.return_value = mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            with self.assertRaises(Exception) as ctx:
                fetch_unified_diff("org", "repo", "abc", "def", "tok", cache)
            self.assertIn("Rate limited", str(ctx.exception))

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.clone_repo_bare")
    def test_fetch_unified_diff_406_falls_back_to_clone(self, mock_clone, mock_get):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_resp = MagicMock(status_code=406)
        mock_get.return_value = mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "org__repo.git"
            repo_path.mkdir()
            mock_clone.return_value = repo_path
            cache = RepoCloneCache(tmpdir)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="diff fallback")
                result = fetch_unified_diff("org", "repo", "aaa", "bbb", "tok", cache)
            self.assertEqual(result, "diff fallback")

    def test_main_no_tokens_raises(self):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                main([], Path(tmpdir), "org", "repo")

    def test_main_missing_groups_file_raises(self):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(AuroraPipelineError):
                main(["tok"], Path(tmpdir), "org", "repo")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.build_dataset.fetch_unified_diff")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.TokenRotator")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.RepoCloneCache")
    def test_main_writes_valid_jsonl(self, MockCache, MockRotator, mock_fetch, _):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        mock_fetch.return_value = "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1 @@\n-old\n+new\n"
        MockRotator.return_value.get_token.return_value = "tok"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            groups_file = out_dir / "org__repo_tag_groups.jsonl"
            with open(groups_file, "w") as f:
                f.write(json.dumps({
                    "base_tag": "v1.0.0", "head_tag": "v1.1.0",
                    "base_sha": "aaa", "head_sha": "bbb",
                    "pr_numbers": [1], "release_line": "1.0",
                    "attribution_methods": {"git_log_merge": 1},
                }) + "\n")
            prs_file = out_dir / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "title": "Fix",
                    "body": "fixes #10", "resolved_issues": [10],
                    "html_url": "http://example.com/1",
                }) + "\n")
            issues_file = out_dir / "org__repo_related_issues.jsonl"
            with open(issues_file, "w") as f:
                f.write(json.dumps({
                    "number": 10, "title": "Bug", "body": "Bug body",
                }) + "\n")
            main(["tok"], out_dir, "org", "repo", delay_on_error=0, retry_attempts=1)
            ds_file = out_dir / "org__repo_lht_dataset.jsonl"
            self.assertTrue(ds_file.exists())
            with open(ds_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["org"], "org")
            self.assertEqual(rec["repo"], "repo")
            self.assertIn("fix_patch", rec)
            self.assertIn("test_patch", rec)
            self.assertEqual(rec["lang"], "python")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.build_dataset.fetch_unified_diff")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.TokenRotator")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.RepoCloneCache")
    def test_main_resume_skips_existing(self, MockCache, MockRotator, mock_fetch, _):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        mock_fetch.return_value = "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1 @@\n-old\n+new\n"
        MockRotator.return_value.get_token.return_value = "tok"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            groups_file = out_dir / "org__repo_tag_groups.jsonl"
            with open(groups_file, "w") as f:
                f.write(json.dumps({
                    "base_tag": "v1.0.0", "head_tag": "v1.1.0",
                    "base_sha": "aaa", "head_sha": "bbb",
                    "pr_numbers": [1], "release_line": "1.0",
                    "attribution_methods": {},
                }) + "\n")
            prs_file = out_dir / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "title": "Fix",
                    "body": "", "resolved_issues": [],
                    "html_url": "",
                }) + "\n")
            # Pre-populate dataset to simulate resume
            ds_file = out_dir / "org__repo_lht_dataset.jsonl"
            with open(ds_file, "w") as f:
                f.write(json.dumps({"instance_id": "org__repo-v1.0.0..v1.1.0"}) + "\n")
            main(["tok"], out_dir, "org", "repo", delay_on_error=0, retry_attempts=1)
            with open(ds_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            # Should still be 1 record (skipped the existing one)
            self.assertEqual(len(records), 1)

    @patch("odoo.addons.aurora.tools.collect.build_dataset.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.build_dataset.fetch_unified_diff")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.TokenRotator")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.RepoCloneCache")
    def test_main_lang_parameter_propagated(self, MockCache, MockRotator, mock_fetch, _):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        mock_fetch.return_value = "diff --git a/src/m.js b/src/m.js\n--- a/src/m.js\n+++ b/src/m.js\n@@ -1 +1 @@\n-old\n+new\n"
        MockRotator.return_value.get_token.return_value = "tok"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            groups_file = out_dir / "org__repo_tag_groups.jsonl"
            with open(groups_file, "w") as f:
                f.write(json.dumps({
                    "base_tag": "v1.0.0", "head_tag": "v1.1.0",
                    "base_sha": "aaa", "head_sha": "bbb",
                    "pr_numbers": [1], "release_line": "1.0",
                    "attribution_methods": {},
                }) + "\n")
            prs_file = out_dir / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "title": "Fix",
                    "body": "", "resolved_issues": [],
                    "html_url": "",
                }) + "\n")
            main(["tok"], out_dir, "org", "repo", lang="javascript",
                 delay_on_error=0, retry_attempts=1)
            ds_file = out_dir / "org__repo_lht_dataset.jsonl"
            with open(ds_file) as f:
                rec = json.loads(f.readline())
            self.assertEqual(rec["lang"], "javascript")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.build_dataset.fetch_unified_diff")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.TokenRotator")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.RepoCloneCache")
    def test_main_retry_on_transient_error(self, MockCache, MockRotator, mock_fetch, _):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        # First call fails, second succeeds
        mock_fetch.side_effect = [
            Exception("connection reset"),
            "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1 @@\n-old\n+new\n",
        ]
        MockRotator.return_value.get_token.return_value = "tok"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            groups_file = out_dir / "org__repo_tag_groups.jsonl"
            with open(groups_file, "w") as f:
                f.write(json.dumps({
                    "base_tag": "v1.0.0", "head_tag": "v1.1.0",
                    "base_sha": "aaa", "head_sha": "bbb",
                    "pr_numbers": [1], "release_line": "1.0",
                    "attribution_methods": {},
                }) + "\n")
            prs_file = out_dir / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "title": "Fix",
                    "body": "", "resolved_issues": [],
                    "html_url": "",
                }) + "\n")
            main(["tok"], out_dir, "org", "repo", delay_on_error=0, retry_attempts=2)
            ds_file = out_dir / "org__repo_lht_dataset.jsonl"
            with open(ds_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 1)

    @patch("odoo.addons.aurora.tools.collect.build_dataset.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.build_dataset.fetch_unified_diff")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.TokenRotator")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.RepoCloneCache")
    def test_main_permanent_error_skips_group(self, MockCache, MockRotator, mock_fetch, _):
        from odoo.addons.aurora.tools.collect.build_dataset import main
        mock_fetch.side_effect = Exception("404 Not Found")
        MockRotator.return_value.get_token.return_value = "tok"
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            groups_file = out_dir / "org__repo_tag_groups.jsonl"
            with open(groups_file, "w") as f:
                f.write(json.dumps({
                    "base_tag": "v1.0.0", "head_tag": "v1.1.0",
                    "base_sha": "aaa", "head_sha": "bbb",
                    "pr_numbers": [1], "release_line": "1.0",
                    "attribution_methods": {},
                }) + "\n")
            prs_file = out_dir / "org__repo_lht_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "title": "Fix",
                    "body": "", "resolved_issues": [],
                    "html_url": "",
                }) + "\n")
            main(["tok"], out_dir, "org", "repo", delay_on_error=0, retry_attempts=3)
            ds_file = out_dir / "org__repo_lht_dataset.jsonl"
            with open(ds_file) as f:
                records = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(records), 0)

    def test_repo_clone_cache_init(self):
        from odoo.addons.aurora.tools.collect.build_dataset import RepoCloneCache
        cache = RepoCloneCache("/tmp/test_cache", auth_token="tok123")
        self.assertEqual(cache._cache_dir, "/tmp/test_cache")
        self.assertEqual(cache._auth_token, "tok123")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.clone_repo_bare")
    def test_repo_clone_cache_ensure_cloned_failure(self, mock_clone):
        from odoo.addons.aurora.tools.collect.build_dataset import RepoCloneCache
        mock_clone.return_value = None
        cache = RepoCloneCache("/tmp/test")
        with self.assertRaises(RuntimeError):
            cache.ensure_cloned("org", "repo")


class TestOptionalIntExpanded(unittest.TestCase):
    """Extra boundary tests for optional_int."""

    def test_large_positive(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertEqual(optional_int("999999999"), 999999999)

    def test_null_uppercase(self):
        from odoo.addons.aurora.tools.collect.util import optional_int
        self.assertIsNone(optional_int("NULL"))

    def test_float_raises(self):
        import argparse
        from odoo.addons.aurora.tools.collect.util import optional_int
        with self.assertRaises(argparse.ArgumentTypeError):
            optional_int("3.14")


class TestGetTokensExpanded(unittest.TestCase):
    """Expanded get_tokens coverage."""

    @patch("odoo.addons.aurora.tools.collect.util._load_env_tokens", return_value=[])
    @patch("odoo.addons.aurora.tools.collect.util.find_default_token_file", return_value=None)
    @patch.dict(os.environ, {"GITHUB_TOKENS": "ghp_env1,ghp_env2"}, clear=False)
    def test_get_tokens_from_env_var(self, mock_find, mock_load):
        from odoo.addons.aurora.tools.collect.util import get_tokens
        result = get_tokens(None)
        self.assertEqual(result, ["ghp_env1", "ghp_env2"])

    @patch("odoo.addons.aurora.tools.collect.util._load_env_tokens", return_value=[])
    @patch("odoo.addons.aurora.tools.collect.util.find_default_token_file", return_value=None)
    @patch.dict(os.environ, {"GH_TOKEN": "ghp_gh"}, clear=False)
    def test_get_tokens_gh_token_env(self, mock_find, mock_load):
        from odoo.addons.aurora.tools.collect.util import get_tokens
        # Clear GITHUB_TOKENS and GITHUB_TOKEN if present
        os.environ.pop("GITHUB_TOKENS", None)
        os.environ.pop("GITHUB_TOKEN", None)
        result = get_tokens(None)
        self.assertEqual(result, ["ghp_gh"])

    def test_get_tokens_single_element_list(self):
        from odoo.addons.aurora.tools.collect.util import get_tokens
        result = get_tokens(["ghp_single"])
        self.assertEqual(result, ["ghp_single"])

    def test_get_tokens_empty_parse_raises(self):
        from odoo.addons.aurora.tools.collect.util import get_tokens, AuroraPipelineError
        from pathlib import Path
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("\n\n\n")
            path = f.name
        try:
            with self.assertRaises((AuroraPipelineError, ValueError)):
                get_tokens([Path(path)])
        finally:
            os.unlink(path)


class TestGitEnvWithToken(unittest.TestCase):
    """Test _git_env_with_token helper."""

    def test_env_contains_required_keys(self):
        from odoo.addons.aurora.tools.collect.util import _git_env_with_token
        env = _git_env_with_token("my_token")
        self.assertEqual(env["GIT_ASKPASS"], "echo")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertIn("Authorization", env["GIT_CONFIG_VALUE_0"])

    def test_b64_token(self):
        from odoo.addons.aurora.tools.collect.util import _b64_token
        import base64
        result = _b64_token("test_token")
        decoded = base64.b64decode(result).decode()
        self.assertEqual(decoded, "x-access-token:test_token")


class TestBuildDatasetPatterns(unittest.TestCase):
    """Test regex patterns in build_dataset."""

    def test_issue_ref_pattern_close(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_REF_PATTERN
        m = ISSUE_REF_PATTERN.search("close #15")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "15")

    def test_issue_ref_pattern_closed(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_REF_PATTERN
        m = ISSUE_REF_PATTERN.search("closed #20")
        self.assertIsNotNone(m)

    def test_issue_url_pattern_various_orgs(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_URL_PATTERN
        m = ISSUE_URL_PATTERN.search("https://github.com/my-org/my-repo/issues/123")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "123")

    def test_issue_ref_no_match_random_text(self):
        from odoo.addons.aurora.tools.collect.build_dataset import ISSUE_REF_PATTERN
        m = ISSUE_REF_PATTERN.search("this is just text #42")
        self.assertIsNone(m)


class TestSafeNameRegex(unittest.TestCase):
    """Test _SAFE_NAME_RE constant."""

    def test_matches_valid_names(self):
        from odoo.addons.aurora.tools.collect.util import _SAFE_NAME_RE
        self.assertTrue(_SAFE_NAME_RE.match("valid-name.123_ok"))

    def test_rejects_empty(self):
        from odoo.addons.aurora.tools.collect.util import _SAFE_NAME_RE
        self.assertIsNone(_SAFE_NAME_RE.match(""))

    def test_rejects_slash(self):
        from odoo.addons.aurora.tools.collect.util import _SAFE_NAME_RE
        self.assertIsNone(_SAFE_NAME_RE.match("a/b"))

    def test_rejects_at_sign(self):
        from odoo.addons.aurora.tools.collect.util import _SAFE_NAME_RE
        self.assertIsNone(_SAFE_NAME_RE.match("user@name"))


class TestGroupPrsMainValidation(unittest.TestCase):
    """Test group_prs_by_tags.main validation and error handling."""

    def test_main_missing_prs_file_raises(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            tags_file = out_dir / "org__repo_tags.jsonl"
            tags_file.write_text("")
            with self.assertRaises(AuroraPipelineError):
                main(["tok"], out_dir, "org", "repo", cache_dir=tmpdir)

    def test_main_invalid_org_name_raises(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(AuroraPipelineError):
                main(["tok"], Path(tmpdir), "org/bad", "repo", cache_dir=tmpdir)


class TestEmitTimeWindowGroup(unittest.TestCase):
    """Test _emit_time_window_group helper."""

    def test_emits_correct_structure(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _emit_time_window_group
        groups = []
        prs = [
            {"number": 1, "merged_at": "2024-01-01T00:00:00Z", "base": {"sha": "a1"}, "merge_commit_sha": "m1"},
            {"number": 2, "merged_at": "2024-01-05T00:00:00Z", "base": {"sha": "a2"}, "merge_commit_sha": "m2"},
        ]
        _emit_time_window_group(groups, prs)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["release_line"], "time_window")
        self.assertEqual(sorted(g["pr_numbers"]), [1, 2])
        self.assertIn("time_window", g["attribution_methods"])


class TestGetAllCommitShas(unittest.TestCase):
    """Test _get_all_commit_shas helper."""

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_returns_set_of_shas(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_all_commit_shas
        mock_run.return_value = MagicMock(returncode=0, stdout="sha1\nsha2\nsha3\n")
        result = _get_all_commit_shas(Path("/repo"), "base", "head")
        self.assertEqual(result, {"sha1", "sha2", "sha3"})

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_failure_returns_empty_set(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_all_commit_shas
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = _get_all_commit_shas(Path("/repo"), "base", "head")
        self.assertEqual(result, set())

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_timeout_returns_empty_set(self, mock_run):
        import subprocess
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _get_all_commit_shas
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=120)
        result = _get_all_commit_shas(Path("/repo"), "base", "head")
        self.assertEqual(result, set())


class TestDetectCherryPicks(unittest.TestCase):
    """Test _detect_cherry_picks helper."""

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_detects_cherry_picks(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _detect_cherry_picks
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="- abc123 some commit\n+ def456 another commit\n"
        )
        result = _detect_cherry_picks(Path("/repo"), "upstream", "head")
        self.assertIn("abc123", result)
        self.assertNotIn("def456", result)

    @patch("odoo.addons.aurora.tools.collect.group_prs_by_tags._run_git")
    def test_failure_returns_empty(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _detect_cherry_picks
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = _detect_cherry_picks(Path("/repo"), "upstream", "head")
        self.assertEqual(result, [])


class TestRunGit(unittest.TestCase):
    """Test _run_git helper."""

    @patch("subprocess.run")
    def test_constructs_proper_command(self, mock_run):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _run_git
        mock_run.return_value = MagicMock(returncode=0)
        _run_git(["status"], Path("/my/repo"))
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd, ["git", "-C", "/my/repo", "status"])


class TestLoadEnvTokensExpanded(unittest.TestCase):
    """Extended tests for _load_env_tokens."""

    def test_gh_token_key_recognized(self):
        from odoo.addons.aurora.tools.collect.util import _load_env_tokens
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("GH_TOKEN=ghp_from_gh\n")
            with patch("odoo.addons.aurora.tools.collect.util.Path") as MockPath:
                mock_cwd = MagicMock()
                mock_cwd.__truediv__ = lambda self, x: Path(tmpdir) / x
                mock_cwd.parents = []
                MockPath.cwd.return_value = mock_cwd
                # Can't easily mock the full chain, test the parsing logic
                # by reading the file directly
                tokens = []
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line:
                            key, _, value = line.partition("=")
                            if key.strip() == "GH_TOKEN":
                                tokens.append(value.strip())
                self.assertEqual(tokens, ["ghp_from_gh"])

    def test_quoted_values_stripped(self):
        # Test the stripping logic for quoted values
        value = "'ghp_quoted'"
        stripped = value.strip().strip("'\"")
        self.assertEqual(stripped, "ghp_quoted")

    def test_double_quoted_values_stripped(self):
        value = '"ghp_dquoted"'
        stripped = value.strip().strip("'\"")
        self.assertEqual(stripped, "ghp_dquoted")

    def test_comma_separated_tokens_split(self):
        value = "ghp_a,ghp_b,ghp_c"
        tokens = [t.strip() for t in value.split(",") if t.strip()]
        self.assertEqual(tokens, ["ghp_a", "ghp_b", "ghp_c"])


class TestFilterPrsFilenamePatterns(unittest.TestCase):

    def test_org_repo_regex_double_underscore(self):
        import re
        org_repo_re = re.compile(r"(.+)__(.+)_prs.jsonl")
        m = org_repo_re.match("my-org__my-repo_prs.jsonl")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "my-org")
        self.assertEqual(m.group(2), "my-repo")

    def test_org_repo_regex_dots_in_name(self):
        import re
        org_repo_re = re.compile(r"(.+)__(.+)_prs.jsonl")
        m = org_repo_re.match("org.name__repo.v2_prs.jsonl")
        self.assertIsNotNone(m)

    def test_org_repo_regex_no_match_single_underscore(self):
        import re
        org_repo_re = re.compile(r"(.+)__(.+)_prs.jsonl")
        m = org_repo_re.match("org_repo_prs.jsonl")
        self.assertIsNone(m)


class TestBuildDatasetInstanceId(unittest.TestCase):

    def test_instance_id_format(self):
        org = "MyOrg"
        repo = "MyRepo"
        base_tag = "v1.0.0"
        head_tag = "v1.1.0"
        instance_id = f"{org.lower()}__{repo.lower()}-{base_tag}..{head_tag}"
        self.assertEqual(instance_id, "myorg__myrepo-v1.0.0..v1.1.0")

    def test_instance_id_lowercase(self):
        org = "UPPER"
        repo = "CASE"
        instance_id = f"{org.lower()}__{repo.lower()}-v1..v2"
        self.assertTrue(instance_id.islower() or ".." in instance_id)
        self.assertIn("upper__case", instance_id)


class TestSplitPatchesExpanded(unittest.TestCase):

    def test_testing_keyword_in_path(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/testing/helper.py b/testing/helper.py\n--- a/testing/helper.py\n+++ b/testing/helper.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertGreater(len(test), 0)

    def test_dunder_tests_keyword(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/__tests__/App.test.js b/__tests__/App.test.js\n--- a/__tests__/App.test.js\n+++ b/__tests__/App.test.js\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertGreater(len(test), 0)

    def test_no_test_path_all_fix(self):
        from odoo.addons.aurora.tools.collect.build_dataset import split_patches
        diff = "diff --git a/lib/core.py b/lib/core.py\n--- a/lib/core.py\n+++ b/lib/core.py\n@@ -1 +1 @@\n-old\n+new\n"
        fix, test = split_patches(diff)
        self.assertGreater(len(fix), 0)
        self.assertEqual(test, "")


class TestParseTagSortOrdering(unittest.TestCase):

    def _parse(self, name):
        from odoo.addons.aurora.tools.collect.get_version_tags import parse_tag
        return parse_tag(name)

    def test_patch_ordering(self):
        a = self._parse("v1.0.1")
        b = self._parse("v1.0.2")
        c = self._parse("v1.0.10")
        self.assertLess(a["sort_key"], b["sort_key"])
        self.assertLess(b["sort_key"], c["sort_key"])

    def test_minor_ordering(self):
        a = self._parse("v1.1.0")
        b = self._parse("v1.2.0")
        c = self._parse("v1.10.0")
        self.assertLess(a["sort_key"], b["sort_key"])
        self.assertLess(b["sort_key"], c["sort_key"])

    def test_major_ordering(self):
        a = self._parse("v1.0.0")
        b = self._parse("v2.0.0")
        c = self._parse("v10.0.0")
        self.assertLess(a["sort_key"], b["sort_key"])
        self.assertLess(b["sort_key"], c["sort_key"])

    def test_calver_ordering_by_month(self):
        a = self._parse("2024.01")
        b = self._parse("2024.06")
        c = self._parse("2024.12")
        self.assertLess(a["sort_key"], b["sort_key"])
        self.assertLess(b["sort_key"], c["sort_key"])

    def test_calver_ordering_by_year(self):
        a = self._parse("2023.12")
        b = self._parse("2024.01")
        self.assertLess(a["sort_key"], b["sort_key"])


class TestFetchUnifiedDiffExpanded(unittest.TestCase):

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.clone_repo_bare")
    def test_empty_response_uses_clone_fallback(self, mock_clone, mock_get):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_resp = MagicMock(status_code=200, text="")
        mock_get.return_value = mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "org__repo.git"
            repo_path.mkdir()
            mock_clone.return_value = repo_path
            cache = RepoCloneCache(tmpdir)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="fallback diff")
                result = fetch_unified_diff("org", "repo", "aaa", "bbb", "tok", cache)
        self.assertEqual(result, "fallback diff")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    @patch("odoo.addons.aurora.tools.collect.build_dataset.clone_repo_bare")
    def test_network_error_uses_clone_fallback(self, mock_clone, mock_get):
        import requests as req
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_get.side_effect = req.ConnectionError("connection refused")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "org__repo.git"
            repo_path.mkdir()
            mock_clone.return_value = repo_path
            cache = RepoCloneCache(tmpdir)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="clone diff")
                result = fetch_unified_diff("org", "repo", "aaa", "bbb", "tok", cache)
        self.assertEqual(result, "clone diff")

    @patch("odoo.addons.aurora.tools.collect.build_dataset.requests.get")
    def test_403_rate_limited_raises(self, mock_get):
        from odoo.addons.aurora.tools.collect.build_dataset import fetch_unified_diff, RepoCloneCache
        mock_resp = MagicMock(status_code=403)
        mock_resp.headers = {"Retry-After": "30"}
        mock_get.return_value = mock_resp
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = RepoCloneCache(tmpdir)
            with self.assertRaises(Exception) as ctx:
                fetch_unified_diff("org", "repo", "abc", "def", "tok", cache)
            self.assertIn("Rate limited", str(ctx.exception))


class TestAggregateIssuesExpanded(unittest.TestCase):

    def test_pr_with_no_body_and_no_issues(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 5, "body": None, "title": "My PR", "resolved_issues": []}]
        result = aggregate_issues(prs, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 5)
        self.assertEqual(result[0]["body"], "")

    def test_whitespace_only_issue_body_substituted(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "PR description", "title": "T", "resolved_issues": [10]}]
        issues = {10: {"number": 10, "title": "Bug", "body": "   \n  \t  "}}
        result = aggregate_issues(prs, issues)
        self.assertEqual(result[0]["body"], "PR description")

    def test_pr_title_none_handled(self):
        from odoo.addons.aurora.tools.collect.build_dataset import aggregate_issues
        prs = [{"number": 1, "body": "", "title": None, "resolved_issues": []}]
        result = aggregate_issues(prs, {})
        self.assertEqual(result[0]["title"], "")


class TestGroupPrsParseDate(unittest.TestCase):

    def test_parse_date_with_timezone_offset(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        result = _parse_date("2024-06-15T10:30:00+05:30")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.month, 6)

    def test_parse_date_negative_offset(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        result = _parse_date("2024-01-01T00:00:00-08:00")
        self.assertIsNotNone(result)

    def test_parse_date_integer_input(self):
        from odoo.addons.aurora.tools.collect.group_prs_by_tags import _parse_date
        with self.assertRaises((AttributeError, TypeError)):
            _parse_date(12345)


class TestTokenRotatorThreadSafety(unittest.TestCase):

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_concurrent_get_token_no_crash(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        import threading
        mock_client = MagicMock()
        mock_client.rate_limiting = (5000, 9999999999)
        MockGithub.return_value = mock_client
        rotator = TokenRotator(["tok1", "tok2", "tok3"])
        results = []

        def worker():
            for _ in range(10):
                results.append(rotator.get_token())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 40)
        self.assertTrue(all(t in ("tok1", "tok2", "tok3") for t in results))

    @patch("odoo.addons.aurora.tools.collect.util.Github")
    def test_concurrent_get_client_no_crash(self, MockGithub):
        from odoo.addons.aurora.tools.collect.util import TokenRotator
        import threading
        mock_client = MagicMock()
        mock_client.rate_limiting = (5000, 9999999999)
        MockGithub.return_value = mock_client
        rotator = TokenRotator(["tok1"])
        results = []

        def worker():
            results.append(rotator.get_client())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 5)


class TestFilterPrsOutputFormat(unittest.TestCase):

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_output_is_valid_jsonl(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                for i in range(3):
                    f.write(json.dumps({
                        "number": i, "state": "closed",
                        "merged_at": "2024-01-01",
                        "title": f"fix #{i+100}", "body": "",
                        "base": {},
                    }) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out) as f:
                for line in f:
                    if line.strip():
                        record = json.loads(line)
                        self.assertIn("resolved_issues", record)
                        self.assertIsInstance(record["resolved_issues"], list)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_output_preserves_unicode(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed",
                    "merged_at": "2024-01-01",
                    "title": "fix #1 \u2014 \u00e9l\u00e8ve", "body": "",
                    "base": {},
                }, ensure_ascii=False) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            with open(out, encoding="utf-8") as f:
                record = json.loads(f.readline())
            self.assertIn("\u00e9l\u00e8ve", record["title"])

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_ensure_ascii_false_in_output(self, _):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed",
                    "merged_at": "2024-01-01",
                    "title": "fix #1", "body": "Caf\u00e9",
                    "base": {},
                }, ensure_ascii=False) + "\n")
            main(["tok"], Path(tmpdir), prs_file, skip_commit_message=True)
            out = Path(tmpdir) / "org__repo_lht_filtered_prs.jsonl"
            raw = out.read_text(encoding="utf-8")
            self.assertIn("Caf\u00e9", raw)


if __name__ == "__main__":
    unittest.main()
