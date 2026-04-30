# -*- coding: utf-8 -*-
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock


class TestGetAllPrsMain(TestCase):

    @patch("odoo.addons.aurora.tools.collect.get_all_prs.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_all_prs.TokenRotator")
    def test_main_writes_jsonl(self, MockRotator, mock_tqdm):
        from odoo.addons.aurora.tools.collect.get_all_prs import main
        mock_client = MagicMock()
        mock_pr = MagicMock()
        mock_pr.number = 1
        mock_pr.state = "closed"
        mock_pr.title = "Fix bug"
        mock_pr.body = "Body"
        mock_pr.url = "http://url"
        mock_pr.id = 123
        mock_pr.node_id = "node_123"
        mock_pr.html_url = "http://html"
        mock_pr.diff_url = "http://diff"
        mock_pr.patch_url = "http://patch"
        mock_pr.issue_url = "http://issue"
        mock_pr.created_at = None
        mock_pr.updated_at = None
        mock_pr.closed_at = None
        mock_pr.merged_at = None
        mock_pr.merge_commit_sha = "abc"
        mock_pr.labels = []
        mock_pr.draft = False
        mock_pr.commits_url = ""
        mock_pr.review_comments_url = ""
        mock_pr.review_comment_url = ""
        mock_pr.comments_url = ""
        mock_pr.base = MagicMock()
        mock_pr.base.raw_data = {"ref": "main"}
        mock_client.get_repo.return_value.get_pulls.return_value = [mock_pr]
        MockRotator.return_value.get_client.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmpdir:
            main(["ghp_test"], Path(tmpdir), "org", "repo")
            out_file = Path(tmpdir) / "org__repo_prs.jsonl"
            self.assertTrue(out_file.exists())
            with open(out_file) as f:
                data = json.loads(f.readline())
            self.assertEqual(data["number"], 1)
            self.assertEqual(data["org"], "org")
            self.assertEqual(data["repo"], "repo")

    def test_get_parser(self):
        from odoo.addons.aurora.tools.collect.get_all_prs import get_parser
        parser = get_parser()
        self.assertIsNotNone(parser)


class TestGetRelatedIssuesMain(TestCase):

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    @patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator")
    def test_main_fetches_issues(self, MockRotator, mock_tqdm):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        mock_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.number = 10
        mock_issue.state = "closed"
        mock_issue.title = "Bug"
        mock_issue.body = "Bug desc"
        mock_client.get_repo.return_value.get_issue.return_value = mock_issue
        MockRotator.return_value.get_client.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"resolved_issues": [10]}) + "\n")
            main(["ghp_test"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            self.assertTrue(out_file.exists())
            with open(out_file) as f:
                data = json.loads(f.readline())
            self.assertEqual(data["number"], 10)

    @patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x)
    def test_main_no_issues_writes_empty(self, mock_tqdm):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"resolved_issues": []}) + "\n")
            main(["ghp_test"], Path(tmpdir), prs_file)
            out_file = Path(tmpdir) / "org__repo_related_issues.jsonl"
            self.assertTrue(out_file.exists())
            self.assertEqual(out_file.read_text(), "")

    def test_main_invalid_filename_raises(self):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        from odoo.addons.aurora.tools.collect.util import AuroraPipelineError
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad_name.jsonl"
            bad_file.write_text("")
            with self.assertRaises(AuroraPipelineError):
                main(["ghp_test"], Path(tmpdir), bad_file)

    def test_main_handles_dict_issues(self):
        from odoo.addons.aurora.tools.collect.get_related_issues import main
        with tempfile.TemporaryDirectory() as tmpdir:
            prs_file = Path(tmpdir) / "org__repo_filtered_prs.jsonl"
            with open(prs_file, "w") as f:
                f.write(json.dumps({"resolved_issues": [{"number": 5}]}) + "\n")
            with patch("odoo.addons.aurora.tools.collect.get_related_issues.TokenRotator") as MockRot:
                mock_client = MagicMock()
                mock_issue = MagicMock(number=5, state="open", title="T", body="B")
                mock_client.get_repo.return_value.get_issue.return_value = mock_issue
                MockRot.return_value.get_client.return_value = mock_client
                with patch("odoo.addons.aurora.tools.collect.get_related_issues.tqdm", side_effect=lambda x, **kw: x):
                    main(["ghp_test"], Path(tmpdir), prs_file)
