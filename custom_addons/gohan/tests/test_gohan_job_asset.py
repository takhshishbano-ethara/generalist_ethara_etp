"""Tests for the extraction-review gate: gohan.job.asset materialization,
the action_generate_prd manual trigger, and uploaded-SVG auto-capture."""

import base64

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import GohanTestCase


@tagged("post_install", "-at_install", "gohan")
class TestMaterializeAssets(GohanTestCase):

    def test_classifies_screenshots_snapshots_and_svgs(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        svg_manifest = {
            "kept": [
                {
                    "filename": "icon_01.svg",
                    "markup": "<svg><path/></svg>",
                    "copyright_safe": True,
                },
            ],
        }
        job._materialize_assets(
            screenshot_keys=[
                "gohan/1/screenshots/00_home.png",
                "gohan/1/screenshots/sections/01_hero.png",
            ],
            asset_keys=[
                "gohan/1/assets/svgs/icon_01.svg",
                "gohan/1/assets/fonts/inter.woff2",
                "gohan/1/assets/images/photo.webp",
            ],
            svg_manifest=svg_manifest,
        )
        by_type = {}
        for asset in job.asset_ids:
            by_type.setdefault(asset.asset_type, []).append(asset)
        self.assertEqual(len(by_type.get("screenshot", [])), 1)
        self.assertEqual(len(by_type.get("section_snapshot", [])), 1)
        self.assertEqual(len(by_type.get("svg", [])), 1)
        self.assertEqual(len(by_type.get("font", [])), 1)
        self.assertEqual(len(by_type.get("image", [])), 1)
        svg = by_type["svg"][0]
        self.assertIn("<svg", svg.content_text)
        self.assertTrue(svg.is_copyright_safe)
        # Fonts are review-only — never auto-fed to the PRD.
        self.assertFalse(by_type["font"][0].include_in_prd)

    def test_preserves_operator_uploads_on_rematerialize(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        uploaded = self.env["gohan.job.asset"].create({
            "job_id": job.id,
            "name": "my-icon.svg",
            "asset_type": "svg",
            "source": "uploaded",
        })
        job._materialize_assets(
            screenshot_keys=["gohan/1/screenshots/00_home.png"],
        )
        self.assertIn(uploaded, job.asset_ids)
        self.assertEqual(
            len(job.asset_ids.filtered(lambda a: a.source == "extracted")), 1
        )

    def test_rematerialize_replaces_old_extracted_rows(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        job._materialize_assets(screenshot_keys=["gohan/1/screenshots/a.png"])
        job._materialize_assets(screenshot_keys=["gohan/1/screenshots/b.png"])
        self.assertEqual(len(job.asset_ids), 1)
        self.assertEqual(job.asset_ids.name, "b.png")


@tagged("post_install", "-at_install", "gohan")
class TestActionGeneratePrd(GohanTestCase):

    def test_generate_prd_from_extracted_advances_state(self):
        job = self._create_job(
            user_id=self.tasker.id, state="extracted",
            prd_prompt="extracted data",
        )
        with self._patch_submit_bg():
            job.action_generate_prd()
        self.assertEqual(job.state, "generating")

    def test_generate_prd_rejected_outside_extracted(self):
        job = self._create_job(
            user_id=self.tasker.id, state="draft", prd_prompt="x",
        )
        with self.assertRaises(UserError):
            job.action_generate_prd()

    def test_generate_prd_requires_extraction_data(self):
        job = self._create_job(user_id=self.tasker.id, state="extracted")
        with self.assertRaises(UserError):
            job.action_generate_prd()


@tagged("post_install", "-at_install", "gohan")
class TestAssetSvgAutofill(GohanTestCase):

    def test_uploaded_svg_captures_markup(self):
        job = self._create_job(user_id=self.tasker.id, state="extracted")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>'
        asset = self.env["gohan.job.asset"].create({
            "job_id": job.id,
            "name": "up.svg",
            "file_name": "up.svg",
            "file_data": base64.b64encode(svg),
        })
        self.assertEqual(asset.asset_type, "svg")
        self.assertIn("<svg", asset.content_text)
        self.assertEqual(asset.svg_markup(), asset.content_text)
