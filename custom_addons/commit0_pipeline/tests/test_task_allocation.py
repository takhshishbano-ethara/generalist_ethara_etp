# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestTaskAllocation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Eval = cls.env["commit0.repo.evaluation"]
        cls.Wizard = cls.env["commit0.import.repos.wizard"]
        cls.Config = cls.env["ir.config_parameter"].sudo()
        cls.Config.set_param("commit0_pipeline.max_active_tasks", "2")

        cls.admin_group = cls.env.ref("commit0_pipeline.group_commit0_admin")
        cls.user_group = cls.env.ref("commit0_pipeline.group_commit0_user")
        cls.test_user = cls.env["res.users"].create(
            {
                "name": "Test Tasker",
                "login": "test_tasker",
                "groups_id": [(6, 0, [cls.user_group.id])],
            }
        )

    def _make_csv(self, urls):
        content = "\n".join(urls)
        return base64.b64encode(content.encode("utf-8"))

    def test_csv_import_creates_unassigned_records(self):
        csv_data = self._make_csv(
            [
                "https://github.com/owner/repo-a",
                "https://github.com/owner/repo-b",
            ]
        )
        wiz = self.Wizard.create({"csv_file": csv_data, "csv_filename": "test.csv"})
        wiz.action_import()
        recs = self.Eval.search(
            [
                (
                    "repo_url",
                    "in",
                    [
                        "https://github.com/owner/repo-a",
                        "https://github.com/owner/repo-b",
                    ],
                )
            ]
        )
        self.assertEqual(len(recs), 2)
        for rec in recs:
            self.assertFalse(rec.user_id)
            self.assertEqual(rec.current_stage, "stage1")

    def test_csv_import_skips_duplicates(self):
        self.Eval.create({"repo_url": "https://github.com/owner/dup-repo"})
        csv_data = self._make_csv(
            [
                "https://github.com/owner/dup-repo",
                "https://github.com/owner/new-repo",
            ]
        )
        wiz = self.Wizard.create({"csv_file": csv_data, "csv_filename": "test.csv"})
        wiz.action_import()
        self.assertIn("Skipped (duplicate): 1", wiz.result_message)
        self.assertIn("Created: 1", wiz.result_message)

    def test_csv_import_no_valid_urls(self):
        csv_data = self._make_csv(["not-a-url", "also-not-a-url"])
        wiz = self.Wizard.create({"csv_file": csv_data, "csv_filename": "test.csv"})
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_start_task_assigns_oldest(self):
        r1 = self.Eval.create(
            {"repo_url": "https://github.com/o/first", "user_id": False}
        )
        r2 = self.Eval.create(
            {"repo_url": "https://github.com/o/second", "user_id": False}
        )
        result = self.Eval.with_user(self.test_user).action_start_task()
        self.assertEqual(result["res_id"], r1.id)
        r1.invalidate_recordset()
        self.assertEqual(r1.user_id.id, self.test_user.id)

    def test_start_task_respects_limit(self):
        self.Config.set_param("commit0_pipeline.max_active_tasks", "1")
        self.Eval.create({"repo_url": "https://github.com/o/pool1", "user_id": False})
        self.Eval.create({"repo_url": "https://github.com/o/pool2", "user_id": False})
        self.Eval.with_user(self.test_user).action_start_task()
        with self.assertRaises(UserError):
            self.Eval.with_user(self.test_user).action_start_task()

    def test_start_task_no_available(self):
        self.Config.set_param("commit0_pipeline.max_active_tasks", "5")
        with self.assertRaises(UserError):
            self.Eval.with_user(self.test_user).action_start_task()

    def test_start_task_picks_any_stage(self):
        r1 = self.Eval.create(
            {
                "repo_url": "https://github.com/o/mid-stage",
                "user_id": False,
                "current_stage": "stage3",
            }
        )
        result = self.Eval.with_user(self.test_user).action_start_task()
        self.assertEqual(result["res_id"], r1.id)

    def test_start_task_skips_terminal(self):
        self.Eval.create(
            {
                "repo_url": "https://github.com/o/done-task",
                "user_id": False,
                "current_stage": "done",
            }
        )
        self.Eval.create(
            {
                "repo_url": "https://github.com/o/failed-task",
                "user_id": False,
                "current_stage": "failed",
            }
        )
        r3 = self.Eval.create(
            {
                "repo_url": "https://github.com/o/active-task",
                "user_id": False,
                "current_stage": "stage2",
            }
        )
        result = self.Eval.with_user(self.test_user).action_start_task()
        self.assertEqual(result["res_id"], r3.id)

    def test_release_task_unassigns(self):
        rec = self.Eval.create(
            {
                "repo_url": "https://github.com/o/release-me",
                "user_id": self.test_user.id,
            }
        )
        rec.action_release_task()
        self.assertFalse(rec.user_id)

    def test_release_preserves_stage(self):
        rec = self.Eval.create(
            {
                "repo_url": "https://github.com/o/mid-release",
                "user_id": self.test_user.id,
                "current_stage": "stage4",
            }
        )
        rec.action_release_task()
        self.assertFalse(rec.user_id)
        self.assertEqual(rec.current_stage, "stage4")

    def test_max_active_tasks_config(self):
        self.Config.set_param("commit0_pipeline.max_active_tasks", "5")
        val = int(self.Config.get_param("commit0_pipeline.max_active_tasks", "1"))
        self.assertEqual(val, 5)
