import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestWebhookHandler(TransactionCase):
    """Test pipeline_webhook controller logic via model methods directly.

    Since Odoo TransactionCase cannot easily invoke HTTP controllers,
    we test the business logic that the controller calls on the model.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/testorg/testrepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
            "pr_collection_status": "running",
        })

    def test_heartbeat_updates_last_heartbeat(self):
        self.assertFalse(self.repo.last_heartbeat)
        self.repo.write({"last_heartbeat": fields.Datetime.now()})
        self.assertTrue(self.repo.last_heartbeat)

    def test_progress_sets_running_status(self):
        self.repo.write({
            "pr_collection_status": "running",
            "pr_collection_step": "Step 2/5: Filtering...",
            "pr_collection_progress": 35.0,
            "last_heartbeat": fields.Datetime.now(),
        })
        self.assertEqual(self.repo.pr_collection_status, "running")
        self.assertEqual(self.repo.pr_collection_progress, 35.0)
        self.assertIn("2/5", self.repo.pr_collection_step)

    def test_done_status_creates_instances_then_marks_done(self):
        dataset = [
            {"org": "testorg", "repo": "testrepo", "number": 1,
             "state": "closed", "title": "Fix A", "body": "Body A",
             "base": {"label": "main", "ref": "main", "sha": "aaa111"},
             "fix_patch": "diff --git a/x.py b/x.py\n-old\n+new\n",
             "test_patch": "diff --git a/test_x.py b/test_x.py\n-old\n+new\n",
             "resolved_issues": []},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for entry in dataset:
                f.write(json.dumps(entry) + "\n")
            tmp_path = f.name

        try:
            self.repo.write({
                "pr_collection_progress": 100,
                "pr_collection_step": "Creating instances from S3...",
                "total_prs_fetched": 10,
                "filtered_prs_count": 5,
                "raw_dataset_count": 1,
            })
            self.repo._create_instances_from_dataset(tmp_path)
            self.repo.write({"pr_collection_status": "done", "pr_collection_step": ""})

            self.assertEqual(self.repo.pr_collection_status, "done")
            self.assertEqual(len(self.repo.instance_ids), 1)
            self.assertEqual(self.repo.instance_ids[0].name, "testorg__testrepo-1")
        finally:
            os.unlink(tmp_path)

    def test_failed_status_sets_error(self):
        self.repo.write({
            "pr_collection_status": "failed",
            "error_message": "No PRs passed filtering for testorg/testrepo",
            "pr_collection_step": "",
        })
        self.assertEqual(self.repo.pr_collection_status, "failed")
        self.assertIn("No PRs", self.repo.error_message)


class TestCreateInstancesFromDataset(TransactionCase):
    """Test _create_instances_from_dataset edge cases."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/edgeorg/edgerepo",
            "language": "python",
            "pipeline_mode": "swe",
            "current_stage": "stage2",
        })

    def _write_jsonl(self, entries):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        f.close()
        return f.name

    def _valid_entry(self, number=1, **overrides):
        entry = {
            "org": "edgeorg", "repo": "edgerepo", "number": number,
            "state": "closed", "title": f"PR #{number}", "body": "desc",
            "base": {"label": "main", "ref": "main", "sha": "abc" + str(number)},
            "fix_patch": "--- a/x.py\n+++ b/x.py\n-a\n+b\n",
            "test_patch": "--- a/t.py\n+++ b/t.py\n-a\n+b\n",
            "resolved_issues": [],
        }
        entry.update(overrides)
        return entry

    def test_null_pr_number_does_not_crash(self):
        path = self._write_jsonl([self._valid_entry(number=None)])
        try:
            self.repo._create_instances_from_dataset(path)
            inst = self.repo.instance_ids
            self.assertEqual(len(inst), 1)
            self.assertEqual(inst[0].pr_number, 0)
        finally:
            os.unlink(path)

    def test_string_pr_number_parsed(self):
        path = self._write_jsonl([self._valid_entry(number="42-57")])
        try:
            self.repo._create_instances_from_dataset(path)
            self.assertEqual(self.repo.instance_ids[0].pr_number, 42)
        finally:
            os.unlink(path)

    def test_oversized_fix_patch_skipped(self):
        big_patch = "x" * (6 * 1024 * 1024)
        path = self._write_jsonl([self._valid_entry(number=99, fix_patch=big_patch)])
        try:
            self.repo._create_instances_from_dataset(path)
            self.assertEqual(len(self.repo.instance_ids), 0)
        finally:
            os.unlink(path)

    def test_duplicate_instances_skipped(self):
        entry = self._valid_entry(number=7)
        path = self._write_jsonl([entry, entry])
        try:
            self.repo._create_instances_from_dataset(path)
            matching = self.repo.instance_ids.filtered(lambda i: i.pr_number == 7)
            self.assertEqual(len(matching), 1)
        finally:
            os.unlink(path)

    def test_empty_lines_skipped(self):
        path = self._write_jsonl([self._valid_entry(number=10)])
        with open(path, "a") as f:
            f.write("\n\n\n")
        try:
            self.repo._create_instances_from_dataset(path)
            self.assertEqual(len(self.repo.instance_ids), 1)
        finally:
            os.unlink(path)

    def test_resolved_issues_created(self):
        entry = self._valid_entry(number=20, resolved_issues=[
            {"number": 5, "title": "Bug report", "body": "It crashes"},
        ])
        path = self._write_jsonl([entry])
        try:
            self.repo._create_instances_from_dataset(path)
            inst = self.repo.instance_ids[0]
            self.assertEqual(len(inst.resolved_issue_ids), 1)
            self.assertEqual(inst.resolved_issue_ids[0].issue_number, 5)
        finally:
            os.unlink(path)


class TestS3UriParsing(TransactionCase):
    """Test S3 URI parsing in _create_instances_from_s3."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/s3org/s3repo",
            "language": "python",
            "pipeline_mode": "swe",
        })
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.s3_bucket", "test-bucket")
        ICP.set_param("jaeger.s3_region", "us-east-1")

    def test_s3_uri_prefix_stripped(self):
        s3_paths = {"raw_dataset": "s3://test-bucket/jaeger/phase1/1/org__repo_raw_dataset.jsonl"}
        with patch("boto3.client") as mock_boto:
            mock_client = MagicMock()
            mock_boto.return_value = mock_client
            mock_client.download_file.side_effect = Exception("test-abort")

            with self.assertRaises(Exception):
                self.repo._create_instances_from_s3(s3_paths)

            call_args = mock_client.download_file.call_args[0]
            self.assertEqual(call_args[0], "test-bucket")
            self.assertEqual(call_args[1], "jaeger/phase1/1/org__repo_raw_dataset.jsonl")

    def test_bare_key_used_as_is(self):
        s3_paths = {"raw_dataset": "jaeger/phase1/1/org__repo_raw_dataset.jsonl"}
        with patch("boto3.client") as mock_boto:
            mock_client = MagicMock()
            mock_boto.return_value = mock_client
            mock_client.download_file.side_effect = Exception("test-abort")

            with self.assertRaises(Exception):
                self.repo._create_instances_from_s3(s3_paths)

            call_args = mock_client.download_file.call_args[0]
            self.assertEqual(call_args[1], "jaeger/phase1/1/org__repo_raw_dataset.jsonl")

    def test_missing_raw_dataset_key_raises(self):
        with self.assertRaises(ValueError):
            self.repo._create_instances_from_s3({})


class TestStageGates(TransactionCase):
    """Test _check_current_gate edge cases."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.env["jaeger.repository"].create({
            "repo_url": "https://github.com/gateorg/gaterepo",
            "language": "python",
            "pipeline_mode": "swe",
        })

    def test_stage2_gate_blocks_done_without_instances(self):
        self.repo.write({
            "current_stage": "stage2",
            "pr_collection_status": "done",
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("No instances", msg)

    def test_stage2_gate_passes_with_instances(self):
        self.repo.write({
            "current_stage": "stage2",
            "pr_collection_status": "done",
        })
        self.env["jaeger.instance"].create({
            "name": "gateorg__gaterepo-1",
            "repository_id": self.repo.id,
            "org": "gateorg",
            "repo": "gaterepo",
            "pr_number": 1,
        })
        ok, msg = self.repo._check_current_gate()
        self.assertTrue(ok)

    def test_terminal_state_blocks_all_stages(self):
        self.repo.write({
            "current_stage": "stage2",
            "pr_collection_status": "done",
            "terminal_state": "repo_not_suitable",
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("Terminal state", msg)

    def test_stage3_gate_requires_images_built(self):
        self.repo.write({
            "current_stage": "stage3",
            "docker_build_status": "done",
            "images_built_count": 0,
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("No images", msg)

    def test_stage4_gate_requires_valid_instances(self):
        self.repo.write({
            "current_stage": "stage4",
            "test_execution_status": "done",
            "instances_valid_count": 0,
        })
        ok, msg = self.repo._check_current_gate()
        self.assertFalse(ok)
        self.assertIn("No valid instances", msg)


class TestValidateRepoErrorHandling(TransactionCase):

    def _create_repo(self):
        count = self.env["jaeger.repository"].search_count([])
        return self.env["jaeger.repository"].create({
            "repo_url": f"https://github.com/errorg/errrepo-{count}",
            "language": "python",
            "pipeline_mode": "swe",
        })

    def test_transient_error_no_terminal_state(self):
        repo = self._create_repo()
        with patch.object(type(repo), "_validate_repo_metadata",
                          side_effect=ConnectionError("Network timeout")):
            try:
                repo.action_validate_repo()
            except UserError:
                pass

        repo.invalidate_recordset()
        self.assertEqual(repo.crawl_status, "failed")
        self.assertEqual(repo.terminal_state, "none")
        self.assertIn("Network timeout", repo.error_message)

    def test_not_found_sets_terminal_state(self):
        try:
            from github import UnknownObjectException
            exc = UnknownObjectException(404, {"message": "Not Found"}, None)
        except ImportError:
            self.skipTest("PyGithub not installed")

        repo = self._create_repo()
        with patch.object(type(repo), "_validate_repo_metadata",
                          side_effect=exc):
            try:
                repo.action_validate_repo()
            except UserError:
                pass

        repo.invalidate_recordset()
        self.assertEqual(repo.crawl_status, "failed")
        self.assertEqual(repo.terminal_state, "repo_not_suitable")

    def test_success_advances_to_stage2(self):
        repo = self._create_repo()
        with patch.object(type(repo), "_validate_repo_metadata"):
            repo.action_validate_repo()

        self.assertEqual(repo.crawl_status, "done")
        self.assertEqual(repo.current_stage, "stage2")


class TestEncryptedTokenParam(TransactionCase):
    """Test get_encrypted_param handles both encrypted and plaintext values."""

    def test_plaintext_value_returned_as_is(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.github_tokens", "ghp_plain_token_1,ghp_plain_token_2")

        from odoo.addons.jaeger.models.credential_manager import get_encrypted_param
        result = get_encrypted_param(self.env, "jaeger.github_tokens")
        self.assertEqual(result, "ghp_plain_token_1,ghp_plain_token_2")

    def test_encrypted_value_decrypted(self):
        from odoo.addons.jaeger.models.credential_manager import (
            encrypt_value, get_encrypted_param,
        )
        ICP = self.env["ir.config_parameter"].sudo()
        plaintext = "ghp_secret_token_abc"
        encrypted = encrypt_value(ICP, plaintext)
        ICP.set_param("jaeger.github_tokens", encrypted)

        result = get_encrypted_param(self.env, "jaeger.github_tokens")
        self.assertEqual(result, plaintext)

    def test_empty_value_returns_empty(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("jaeger.github_tokens", "")

        from odoo.addons.jaeger.models.credential_manager import get_encrypted_param
        result = get_encrypted_param(self.env, "jaeger.github_tokens")
        self.assertEqual(result, "")
