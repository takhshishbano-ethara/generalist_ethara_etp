import base64

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import LeviathanTestCase


@tagged("post_install", "-at_install", "leviathan")
class TestRerunWizard(LeviathanTestCase):

    def test_summary_when_no_extraction(self):
        job = self._create_job(user_id=self.tasker.id, state="done")
        wiz = self.env["leviathan.rerun.wizard"].create({"job_id": job.id})
        self.assertFalse(wiz.has_screenshots)
        self.assertFalse(wiz.has_assets)
        self.assertIn("No extraction data", wiz.asset_summary)

    def test_summary_with_screenshots(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done",
            screenshot_keys=["a/1.png", "a/2.png"],
            asset_keys=["b/c.svg"],
        )
        wiz = self.env["leviathan.rerun.wizard"].create({"job_id": job.id})
        self.assertTrue(wiz.has_screenshots)
        self.assertTrue(wiz.has_assets)
        self.assertIn("2 screenshot", wiz.asset_summary)
        self.assertIn("1 asset", wiz.asset_summary)

    def test_qc_verdict_display(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done", qc_verdict="shippable",
        )
        wiz = self.env["leviathan.rerun.wizard"].create({"job_id": job.id})
        self.assertEqual(wiz.job_qc_verdict, "Shippable")

    def test_score_display_when_unscored(self):
        job = self._create_job(user_id=self.tasker.id, state="done", score=0)
        wiz = self.env["leviathan.rerun.wizard"].create({"job_id": job.id})
        self.assertEqual(wiz.job_score_display, "Not Scored")

    def test_action_rerun_generate_only_delegates(self):
        job = self._create_job(
            user_id=self.tasker.id, state="done", prd_prompt="extracted",
        )
        wiz = self.env["leviathan.rerun.wizard"].create({"job_id": job.id})
        with self._patch_submit_bg():
            wiz.action_rerun_generate_only()
        job.invalidate_recordset()
        self.assertEqual(job.state, "generating")


@tagged("post_install", "-at_install", "leviathan")
class TestImportWizard(LeviathanTestCase):

    def _make_csv(self, content):
        return base64.b64encode(content.encode("utf-8"))

    def test_no_file_raises(self):
        wiz = self.env["leviathan.import.wizard"].create({})
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_missing_url_column_raises(self):
        wiz = self.env["leviathan.import.wizard"].create({
            "csv_file": self._make_csv("foo,bar\n1,2\n"),
            "csv_filename": "test.csv",
        })
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_creates_tasks_with_https_prepend(self):
        wiz = self.env["leviathan.import.wizard"].create({
            "csv_file": self._make_csv("url\nexample.com\nhttp://other.com\n"),
            "csv_filename": "test.csv",
        })
        wiz.action_import()
        jobs = self.Job.search([
            ("url", "in", ["https://example.com", "http://other.com"]),
        ])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].state, "not_assigned")
        self.assertFalse(jobs[0].user_id)

    def test_duplicate_url_skipped(self):
        self._create_job(url="https://example.com")
        wiz = self.env["leviathan.import.wizard"].create({
            "csv_file": self._make_csv("url\nexample.com\nfresh.com\n"),
            "csv_filename": "test.csv",
        })
        wiz.action_import()
        all_for_example = self.Job.search([("url", "=", "https://example.com")])
        all_for_fresh = self.Job.search([("url", "=", "https://fresh.com")])
        self.assertEqual(len(all_for_example), 1)
        self.assertEqual(len(all_for_fresh), 1)

    def test_category_matched_case_insensitive(self):
        wiz = self.env["leviathan.import.wizard"].create({
            "csv_file": self._make_csv(
                f"url,category\nfoo.com,{self.category.name.upper()}\n"
            ),
            "csv_filename": "test.csv",
        })
        wiz.action_import()
        job = self.Job.search([("url", "=", "https://foo.com")], limit=1)
        self.assertEqual(job.category_id, self.category)

    def test_empty_url_row_skipped(self):
        wiz = self.env["leviathan.import.wizard"].create({
            "csv_file": self._make_csv("url\n\nvalid.com\n"),
            "csv_filename": "test.csv",
        })
        wiz.action_import()
        self.assertTrue(self.Job.search([("url", "=", "https://valid.com")]))


@tagged("post_install", "-at_install", "leviathan")
class TestStartTaskWizard(LeviathanTestCase):

    def test_confirm_without_category_picks_any(self):
        job = self._create_job()
        wiz = self.env["leviathan.start.task.wizard"].create({})
        action = wiz.action_confirm()
        self.assertEqual(action["res_id"], job.id)

    def test_confirm_with_category_filters(self):
        plain = self._create_job(category_id=self.category.id)
        ct = self._create_job(category_id=self.category_ct.id)
        wiz = self.env["leviathan.start.task.wizard"].create({
            "category_id": self.category_ct.id,
        })
        action = wiz.action_confirm()
        self.assertEqual(action["res_id"], ct.id)


@tagged("post_install", "-at_install", "leviathan")
class TestBulkAssignWizard(LeviathanTestCase):

    def test_no_active_ids_raises(self):
        wiz = self.env["leviathan.bulk.assign.wizard"].with_context(
            active_ids=[],
        ).create({"user_id": self.tasker.id})
        with self.assertRaises(UserError):
            wiz.action_assign()

    def test_writes_user_id(self):
        a = self._create_job()
        b = self._create_job()
        wiz = self.env["leviathan.bulk.assign.wizard"].with_context(
            active_ids=[a.id, b.id],
        ).create({"user_id": self.other_user.id})
        wiz.action_assign()
        a.invalidate_recordset()
        b.invalidate_recordset()
        self.assertEqual(a.user_id, self.other_user)
        self.assertEqual(b.user_id, self.other_user)

    def test_task_count_computed(self):
        a = self._create_job()
        b = self._create_job()
        c = self._create_job()
        wiz = self.env["leviathan.bulk.assign.wizard"].with_context(
            active_ids=[a.id, b.id, c.id],
        ).create({"user_id": self.tasker.id})
        self.assertEqual(wiz.task_count, 3)
