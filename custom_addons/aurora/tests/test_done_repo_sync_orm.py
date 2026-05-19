import unittest
from unittest.mock import patch as mock_patch

from odoo.addons.aurora.models import done_repo_sync as drs


class TestDiscoverySkipReasonField(unittest.TestCase):

    def test_skip_reason_default_none(self):
        rec = self.env["aurora.discovery"].create({
            "github_org": "pallets", "github_repo": "flask",
        })
        self.assertEqual(rec.skip_reason, "none")

    def test_skip_reason_selection_values(self):
        rec = self.env["aurora.discovery"].create({
            "github_org": "pallets", "github_repo": "flask",
        })
        for value in (
            "none", "done_exact", "done_bare", "done_fuzzy",
            "no_pr_exact", "no_pr_bare", "no_pr_fuzzy", "manual",
        ):
            rec.write({"skip_reason": value})
            self.assertEqual(rec.skip_reason, value)


class TestCronSyncDoneRepo(unittest.TestCase):

    def setUp(self):
        self.disc_done = self.env["aurora.discovery"].create({
            "github_org": "pallets", "github_repo": "flask",
            "state": "new", "skip_reason": "none",
        })
        self.disc_no_pr = self.env["aurora.discovery"].create({
            "github_org": "torvalds", "github_repo": "linux",
            "state": "new", "skip_reason": "none",
        })
        self.disc_fresh = self.env["aurora.discovery"].create({
            "github_org": "rust-lang", "github_repo": "cargo",
            "state": "new", "skip_reason": "none",
        })
        self.disc_already_promoted = self.env["aurora.discovery"].create({
            "github_org": "celery", "github_repo": "celery",
            "state": "promoted", "skip_reason": "none",
        })

    def test_marks_done_repo(self):
        done_idx = drs._build_match_index({"pallets/flask"})
        no_pr_idx = drs._build_match_index(set())
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", return_value=(done_idx, no_pr_idx),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_done.refresh()
        self.assertEqual(self.disc_done.skip_reason, "done_exact")
        self.assertTrue(self.disc_done.skip_reason_synced_at)

    def test_marks_no_pr(self):
        done_idx = drs._build_match_index(set())
        no_pr_idx = drs._build_match_index({"torvalds/linux"})
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", return_value=(done_idx, no_pr_idx),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_no_pr.refresh()
        self.assertEqual(self.disc_no_pr.skip_reason, "no_pr_exact")

    def test_leaves_fresh_alone(self):
        done_idx = drs._build_match_index({"pallets/flask"})
        no_pr_idx = drs._build_match_index({"torvalds/linux"})
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", return_value=(done_idx, no_pr_idx),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_fresh.refresh()
        self.assertEqual(self.disc_fresh.skip_reason, "none")

    def test_skips_non_new_states(self):
        done_idx = drs._build_match_index({"celery/celery"})
        no_pr_idx = drs._build_match_index(set())
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", return_value=(done_idx, no_pr_idx),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_already_promoted.refresh()
        self.assertEqual(self.disc_already_promoted.skip_reason, "none")

    def test_missing_token_is_logged_not_raised(self):
        with mock_patch.object(
            drs, "get_config",
            side_effect=ValueError("token not set"),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_done.refresh()
        self.assertEqual(self.disc_done.skip_reason, "none")

    def test_sync_failure_does_not_crash(self):
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", side_effect=RuntimeError("github down"),
        ):
            self.env["aurora.discovery"]._cron_sync_done_repo()
        self.disc_done.refresh()
        self.assertEqual(self.disc_done.skip_reason, "none")


class TestActionRefreshDoneList(unittest.TestCase):

    def test_returns_notification(self):
        done_idx = drs._build_match_index(set())
        no_pr_idx = drs._build_match_index(set())
        rec = self.env["aurora.discovery"].create({
            "github_org": "foo", "github_repo": "bar", "state": "new",
        })
        with mock_patch.object(
            drs, "get_config", return_value=("test/repo", "main", "token"),
        ), mock_patch.object(
            drs, "sync_done_repos", return_value=(done_idx, no_pr_idx),
        ), mock_patch.object(drs, "invalidate_cache") as mock_inv:
            result = rec.action_refresh_done_list()
        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        mock_inv.assert_called_once_with("test/repo", "main")


class TestEvaluationPatchFileFallback(unittest.TestCase):

    def test_patch_file_defaults_to_dataset_file(self):
        rec = self.env["aurora.evaluation"].create({
            "dataset_file": "/tmp/test_dataset.jsonl",
        })
        rec.patch_file = False
        self.assertFalse(rec.patch_file)


if __name__ == "__main__":
    unittest.main()
