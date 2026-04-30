# -*- coding: utf-8 -*-
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock


class TestExtractResolvedIssues(TestCase):

    def _pull(self, title="", body="", commits=None):
        return {
            "title": title or "",
            "body": body or "",
            "commits": commits or [],
        }

    def test_fix_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fix #42"))
        self.assertIn(42, result)

    def test_fixes_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fixes #10"))
        self.assertIn(10, result)

    def test_close_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="close #5"))
        self.assertIn(5, result)

    def test_closes_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="closes #99"))
        self.assertIn(99, result)

    def test_resolve_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="resolve #7"))
        self.assertIn(7, result)

    def test_resolves_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="resolves #8"))
        self.assertIn(8, result)

    def test_multiple_issues(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fix #1 fix #2 fix #3"))
        self.assertEqual(set(result), {1, 2, 3})

    def test_title_issues(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(title="fix #50"))
        self.assertIn(50, result)

    def test_commit_message_issues(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(
            commits=[{"message": "fix #30"}]
        ))
        self.assertIn(30, result)

    def test_no_issues(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="no issues here"))
        self.assertEqual(result, [])

    def test_zero_discarded(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fix #0"))
        self.assertEqual(result, [])

    def test_html_comments_stripped(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="<!-- fix #99 --> fix #1"))
        self.assertNotIn(99, result)
        self.assertIn(1, result)

    def test_dedup(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fix #5 fix #5 fix #5"))
        self.assertEqual(result.count(5), 1)

    def test_none_body(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(title=None, body=None))
        self.assertEqual(result, [])

    def test_fixed_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="fixed #22"))
        self.assertIn(22, result)

    def test_closed_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="closed #33"))
        self.assertIn(33, result)

    def test_resolved_keyword(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(body="resolved #44"))
        self.assertIn(44, result)

    def test_combined_title_body_commits(self):
        from odoo.addons.aurora.tools.collect.filter_prs import extract_resolved_issues
        result = extract_resolved_issues(self._pull(
            title="fix #1",
            body="closes #2",
            commits=[{"message": "resolve #3"}],
        ))
        self.assertEqual(set(result), {1, 2, 3})


class TestFilterPrsMain(TestCase):

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_main_filters_merged_closed(self, mock_tqdm):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            out_dir = Path(tmpdir)
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "merged_at": "2024-01-01",
                    "title": "fix #10", "body": "fixes #10", "base": {},
                }) + "\n")
                f.write(json.dumps({
                    "number": 2, "state": "open", "merged_at": None,
                    "title": "", "body": "", "base": {},
                }) + "\n")
            main(["ghp_fake"], out_dir, prs_file, skip_commit_message=True, mode="aurora")
            out_file = out_dir / "org__repo_filtered_prs.jsonl"
            self.assertTrue(out_file.exists())
            with open(out_file) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 1)

    @patch("odoo.addons.aurora.tools.collect.filter_prs.tqdm", side_effect=lambda x, **kw: x)
    def test_main_skips_unmerged(self, mock_tqdm):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({
                    "number": 1, "state": "closed", "merged_at": None,
                    "title": "fix #1", "body": "", "base": {},
                }) + "\n")
            main(["ghp_fake"], Path(tmpdir), prs_file, skip_commit_message=True)
            out_file = Path(tmpdir) / "org__repo_filtered_prs.jsonl"
            with open(out_file) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 0)

    def test_main_invalid_filename_raises(self):
        from odoo.addons.aurora.tools.collect.filter_prs import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "badname.jsonl"
            bad_file.write_text("")
            with self.assertRaises(AuroraPipelineError):
                main(["ghp_fake"], Path(tmpdir), bad_file, skip_commit_message=True)
