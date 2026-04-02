# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRepoEntry(TransactionCase):
    def setUp(self):
        super().setUp()
        self.PipelineRun = self.env["commit0.pipeline.run"]
        self.RepoEntry = self.env["commit0.repo.entry"]
        self.run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )

    def test_computed_name_with_repo_name(self):
        """Entry name shows repo_name when set."""
        entry = self.RepoEntry.create(
            {
                "pipeline_run_id": self.run.id,
                "repo_name": "arrow",
            }
        )
        self.assertEqual(entry.name, "arrow")

    def test_computed_name_without_repo_name(self):
        """Entry name falls back to sequence when no repo_name."""
        entry = self.RepoEntry.create(
            {
                "pipeline_run_id": self.run.id,
                "sequence": 5,
            }
        )
        self.assertEqual(entry.name, "Repo #5")

    def test_default_state_is_pending(self):
        """New entries start in pending state."""
        entry = self.RepoEntry.create(
            {
                "pipeline_run_id": self.run.id,
                "repo_name": "arrow",
            }
        )
        self.assertEqual(entry.state, "pending")

    def test_cascade_delete(self):
        """Entries are deleted when parent run is deleted."""
        entry = self.RepoEntry.create(
            {
                "pipeline_run_id": self.run.id,
                "repo_name": "arrow",
            }
        )
        entry_id = entry.id
        self.run.unlink()
        self.assertFalse(self.RepoEntry.browse(entry_id).exists())
