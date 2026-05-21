from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from .common import LeviathanTestCase


@tagged("post_install", "-at_install", "leviathan")
class TestScoreDisplay(LeviathanTestCase):

    def test_empty_when_score_zero(self):
        job = self._create_job(score=0)
        self.assertEqual(job.score_display, "")

    def test_integer_rendering(self):
        job = self._create_job(score=87.4)
        self.assertEqual(job.score_display, "87")

    def test_truncates_decimal(self):
        job = self._create_job(score=99.49)
        self.assertEqual(job.score_display, "99")


@tagged("post_install", "-at_install", "leviathan")
class TestStageProgress(LeviathanTestCase):

    def test_empty_for_unknown_state(self):
        job = self._create_job()
        self.assertEqual(job.stage_progress_html, "")

    def test_extracting_renders_progress(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracting",
            started_at=fields.Datetime.now() - timedelta(seconds=120),
            last_heartbeat=fields.Datetime.now() - timedelta(seconds=60),
        )
        self.assertIn("Stage", job.stage_progress_html)
        self.assertIn("Est. remaining", job.stage_progress_html)

    def test_done_renders_total_duration(self):
        started = fields.Datetime.now() - timedelta(seconds=300)
        job = self._create_job(
            user_id=self.tasker.id, state="done",
            started_at=started,
            completed_at=fields.Datetime.now(),
            duration_seconds=300,
        )
        self.assertIn("Completed in", job.stage_progress_html)
        self.assertIn("5m", job.stage_progress_html)

    def test_failed_renders_red(self):
        started = fields.Datetime.now() - timedelta(seconds=45)
        job = self._create_job(
            user_id=self.tasker.id, state="failed",
            started_at=started, completed_at=fields.Datetime.now(),
            duration_seconds=45,
        )
        self.assertIn("Failed after", job.stage_progress_html)
        self.assertIn("dc3545", job.stage_progress_html)


@tagged("post_install", "-at_install", "leviathan")
class TestScoreReportHtml(LeviathanTestCase):

    def test_empty_when_no_report(self):
        job = self._create_job()
        self.assertEqual(job.score_report_html, "")

    def test_renders_grade_and_sections(self):
        job = self._create_job(score_report_json={
            "total_score": 87,
            "grade": "B",
            "details": {"word_count": 1500, "tier1_violations": []},
            "section_scores": {
                "S1": {"score": 4, "max": 5},
                "S2": {"score": 10, "max": 14},
            },
            "reject_triggers": [],
            "warnings": [],
        })
        html = job.score_report_html
        self.assertIn("87", html)
        self.assertIn("B", html)
        self.assertIn("S1:", html)
        self.assertIn("S2:", html)

    def test_renders_reject_triggers(self):
        job = self._create_job(score_report_json={
            "total_score": 0,
            "grade": "REJECT",
            "details": {"word_count": 100, "tier1_violations": ["sleek"]},
            "section_scores": {},
            "reject_triggers": ["R4: Under 800 words"],
            "warnings": [],
        })
        html = job.score_report_html
        self.assertIn("R4", html)


@tagged("post_install", "-at_install", "leviathan")
class TestAssetPreviews(LeviathanTestCase):

    def test_no_screenshots_message(self):
        job = self._create_job()
        self.assertIn("No screenshots", job.screenshot_urls_html)

    def test_screenshots_render_when_bucket_set(self):
        self._set_param("leviathan.s3_bucket", "lev-bucket")
        self._set_param("leviathan.s3_region", "us-east-1")
        job = self._create_job(
            screenshot_keys=["leviathan/LEV-001/screenshots/01.png"],
        )
        html = job.screenshot_urls_html
        self.assertIn("<img", html)
        self.assertIn("01.png", html)

    def test_cdn_url_overrides_bucket(self):
        self._set_param("leviathan.s3_bucket", "lev-bucket")
        self._set_param("leviathan.s3_cdn_url", "https://cdn.example.com")
        job = self._create_job(
            screenshot_keys=["leviathan/LEV-001/01.png"],
        )
        self.assertIn("https://cdn.example.com/leviathan/", job.screenshot_urls_html)

    def test_asset_grouping(self):
        self._set_param("leviathan.s3_bucket", "lev-bucket")
        job = self._create_job(asset_keys=[
            "leviathan/LEV-001/deliverables/Page Assets/logo.svg",
            "leviathan/LEV-001/deliverables/References/ref1.png",
            "leviathan/LEV-001/deliverables/_unused/sample.jpg",
        ])
        html = job.asset_urls_html
        self.assertIn("Page Assets (1)", html)
        self.assertIn("References (1)", html)
        self.assertIn("Unused (Copyrighted) (1)", html)


@tagged("post_install", "-at_install", "leviathan")
class TestAssetScoreHtml(LeviathanTestCase):

    def test_empty(self):
        job = self._create_job()
        self.assertEqual(job.asset_score_html, "")

    def test_good_quality(self):
        job = self._create_job(
            screenshot_keys=[f"a/{i}.png" for i in range(8)],
            asset_keys=[
                "x/Page Assets/a.svg", "x/Page Assets/b.svg",
                "x/References/c.png", "x/References/d.png",
            ],
        )
        self.assertIn("Good", job.asset_score_html)

    def test_poor_quality(self):
        job = self._create_job(
            screenshot_keys=["a/1.png"],
            asset_keys=[],
        )
        self.assertIn("Poor", job.asset_score_html)

    def test_partial_quality(self):
        job = self._create_job(
            screenshot_keys=[f"a/{i}.png" for i in range(4)],
            asset_keys=["x/Page Assets/a.svg"],
        )
        self.assertIn("Partial", job.asset_score_html)


@tagged("post_install", "-at_install", "leviathan")
class TestIsAdmin(LeviathanTestCase):

    def test_admin_flag_for_admin_user(self):
        admin_group = self.env.ref("leviathan.group_leviathan_admin")
        admin_group.user_ids = [(4, self.tasker.id)]
        job = self._create_job().with_user(self.tasker)
        self.assertTrue(job.is_admin)

    def test_non_admin_user(self):
        admin_group = self.env.ref("leviathan.group_leviathan_admin")
        admin_group.user_ids = [(3, self.other_user.id)]
        job = self._create_job().with_user(self.other_user)
        self.assertFalse(job.is_admin)
