# -*- coding: utf-8 -*-
import base64
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPipelineRun(TransactionCase):
    def setUp(self):
        super().setUp()
        self.PipelineRun = self.env["commit0.pipeline.run"]

    def test_create_assigns_sequence(self):
        """Pipeline run gets sequence name on create."""
        run = self.PipelineRun.create({"entry_type": "batch"})
        self.assertTrue(run.name.startswith("C0-"))
        self.assertNotEqual(run.name, "New")

    def test_default_state_is_idle(self):
        """New pipeline runs start in idle state."""
        run = self.PipelineRun.create({"entry_type": "batch"})
        self.assertEqual(run.state, "idle")

    def test_single_requires_repo_url(self):
        """Single mode requires a valid GitHub URL."""
        with self.assertRaises(ValidationError):
            self.PipelineRun.create(
                {
                    "entry_type": "single",
                    "repo_url": "",
                }
            )

    def test_single_validates_github_url(self):
        """Single mode rejects invalid GitHub URLs."""
        with self.assertRaises(ValidationError):
            self.PipelineRun.create(
                {
                    "entry_type": "single",
                    "repo_url": "https://gitlab.com/foo/bar",
                }
            )

    def test_single_accepts_valid_url(self):
        """Single mode accepts valid GitHub URLs."""
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        self.assertEqual(run.state, "idle")

    def test_batch_requires_csv(self):
        """Batch mode requires a CSV file."""
        with self.assertRaises(ValidationError):
            self.PipelineRun.create(
                {
                    "entry_type": "batch",
                    "csv_file": False,
                }
            )

    def test_parse_csv(self):
        """CSV parsing extracts correct columns."""
        csv_content = "library_name,Github url,Organization Name,RnD\narrow,https://github.com/arrow-py/arrow,Ethara-Ai,Yes\n"
        run = self.PipelineRun.create(
            {
                "entry_type": "batch",
                "csv_file": base64.b64encode(csv_content.encode("utf-8")),
                "csv_filename": "test.csv",
            }
        )
        rows = run._parse_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["library_name"], "arrow")
        self.assertEqual(rows[0]["github_url"], "https://github.com/arrow-py/arrow")
        self.assertEqual(rows[0]["organization_name"], "Ethara-Ai")

    def test_parse_csv_strips_git_suffix(self):
        """CSV parsing strips .git suffix from URLs."""
        csv_content = (
            "library_name,Github url\narrow,https://github.com/arrow-py/arrow.git\n"
        )
        run = self.PipelineRun.create(
            {
                "entry_type": "batch",
                "csv_file": base64.b64encode(csv_content.encode("utf-8")),
                "csv_filename": "test.csv",
            }
        )
        rows = run._parse_csv()
        self.assertEqual(rows[0]["github_url"], "https://github.com/arrow-py/arrow")

    def test_cancel_sets_state(self):
        """Cancelling a pipeline sets state to cancelled."""
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        run.action_cancel_pipeline()
        self.assertEqual(run.state, "cancelled")

    def test_start_from_invalid_state_raises(self):
        """Starting from non-idle/failed state raises."""
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        run.state = "complete"
        with self.assertRaises(ValidationError):
            run.action_start_pipeline()

    def test_progress_pct(self):
        """Progress percentage reflects completed entries."""
        run = self.PipelineRun.create(
            {
                "entry_type": "single",
                "repo_url": "https://github.com/arrow-py/arrow",
            }
        )
        entry1 = self.env["commit0.repo.entry"].create(
            {
                "pipeline_run_id": run.id,
                "repo_name": "arrow",
                "state": "complete",
            }
        )
        entry2 = self.env["commit0.repo.entry"].create(
            {
                "pipeline_run_id": run.id,
                "repo_name": "httpx",
                "state": "pending",
            }
        )
        run.invalidate_recordset()
        self.assertEqual(run.progress_pct, 50.0)
