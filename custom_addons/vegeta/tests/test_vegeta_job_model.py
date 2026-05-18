from psycopg2.errors import CheckViolation

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import VegetaTestCase


@tagged("post_install", "-at_install", "vegeta")
class TestJobCreate(VegetaTestCase):

    def test_sequence_assigned_when_name_is_new(self):
        job = self._create_job()
        self.assertNotEqual(job.name, "New")
        self.assertTrue(job.name)

    def test_explicit_name_preserved(self):
        job = self._create_job(name="LEV-CUSTOM")
        self.assertEqual(job.name, "LEV-CUSTOM")

    def test_default_state_is_not_assigned(self):
        job = self._create_job()
        self.assertEqual(job.state, "not_assigned")

    def test_user_at_creation_auto_promotes_to_draft(self):
        job = self._create_job(user_id=self.tasker.id)
        self.assertEqual(job.state, "draft")

    def test_state_done_without_user_demoted_to_not_assigned(self):
        job = self._create_job(state="done", user_id=False)
        self.assertEqual(job.state, "not_assigned")

    def test_state_draft_without_user_demoted_to_not_assigned(self):
        job = self._create_job(state="draft", user_id=False)
        self.assertEqual(job.state, "not_assigned")

    def test_user_with_explicit_state_keeps_state(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        self.assertEqual(job.state, "extracting")

    @mute_logger("odoo.sql_db")
    def test_url_check_constraint(self):
        with self.assertRaises(Exception):
            with self.cr.savepoint():
                self._create_job(url="")


@tagged("post_install", "-at_install", "vegeta")
class TestJobWrite(VegetaTestCase):

    def test_assigning_user_to_unassigned_promotes_to_draft(self):
        job = self._create_job()
        self.assertEqual(job.state, "not_assigned")
        job.write({"user_id": self.tasker.id})
        self.assertEqual(job.state, "draft")

    def test_clearing_user_from_draft_demotes_to_not_assigned(self):
        job = self._create_job(user_id=self.tasker.id)
        self.assertEqual(job.state, "draft")
        job.write({"user_id": False})
        self.assertEqual(job.state, "not_assigned")

    def test_clearing_user_from_done_does_not_demote(self):
        job = self._create_job(user_id=self.tasker.id)
        job.write({"state": "done"})
        job.write({"user_id": False})
        self.assertEqual(job.state, "done")

    def test_reassigning_user_does_not_change_state(self):
        job = self._create_job(user_id=self.tasker.id, state="extracting")
        job.write({"user_id": self.other_user.id})
        self.assertEqual(job.state, "extracting")
        self.assertEqual(job.user_id.id, self.other_user.id)

    def test_assigning_user_to_not_assigned_with_prd_promotes_to_done(self):
        job = self._create_job(
            user_id=False, prd_text="some PRD content", prd_prompt="extraction",
        )
        self.assertEqual(job.state, "not_assigned")
        job.write({"user_id": self.tasker.id})
        self.assertEqual(job.state, "done")
        self.assertEqual(job.user_id, self.tasker)

    def test_assigning_user_to_not_assigned_with_extraction_only_promotes_to_failed(self):
        job = self._create_job(
            user_id=False, prd_prompt="extracted data", screenshot_keys=["a.png"],
        )
        self.assertEqual(job.state, "not_assigned")
        self.assertFalse(job.prd_text)
        job.write({"user_id": self.tasker.id})
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.user_id, self.tasker)
        self.assertIn("Retry", job.error_message or "")

    def test_assigning_user_to_not_assigned_with_no_data_promotes_to_draft(self):
        job = self._create_job(user_id=False)
        self.assertEqual(job.state, "not_assigned")
        self.assertFalse(job._has_extraction_data)
        job.write({"user_id": self.tasker.id})
        self.assertEqual(job.state, "draft")

    def test_release_then_reassign_done_task_restores_done(self):
        job = self._create_job(
            user_id=self.tasker.id, prd_text="PRD", qc_verdict="shippable",
        )
        job.write({"state": "done"})
        job.action_release_task()
        self.assertEqual(job.state, "not_assigned")
        self.assertFalse(job.user_id)
        self.assertEqual(job.prd_text, "PRD")
        job.write({"user_id": self.other_user.id})
        self.assertEqual(job.state, "done")
        self.assertEqual(job.qc_verdict, "shippable")

    def test_smart_promote_does_not_overwrite_existing_error_message(self):
        job = self._create_job(
            user_id=False, prd_prompt="extracted",
            error_message="original error message",
        )
        job.write({"user_id": self.tasker.id})
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_message, "original error message")


@tagged("post_install", "-at_install", "vegeta")
class TestHasExtractionData(VegetaTestCase):

    def test_empty_job_has_no_extraction_data(self):
        job = self._create_job()
        self.assertFalse(job._has_extraction_data)

    def test_prd_prompt_counts(self):
        job = self._create_job(prd_prompt="some extracted content")
        self.assertTrue(job._has_extraction_data)

    def test_site_discovery_counts(self):
        job = self._create_job(site_discovery_json={"title": "X"})
        self.assertTrue(job._has_extraction_data)

    def test_screenshots_count(self):
        job = self._create_job(screenshot_keys=["a/b.png"])
        self.assertTrue(job._has_extraction_data)

    def test_assets_count(self):
        job = self._create_job(asset_keys=["a/b.svg"])
        self.assertTrue(job._has_extraction_data)


@tagged("post_install", "-at_install", "vegeta")
class TestPromptHelpers(VegetaTestCase):

    def test_prd_prompt_returns_custom_when_set(self):
        self._set_param("vegeta.prd_system_prompt", "CUSTOM PRD PROMPT")
        self.assertEqual(self.Job._get_prd_system_prompt(), "CUSTOM PRD PROMPT")

    def test_prd_prompt_falls_back_to_file(self):
        self._set_param("vegeta.prd_system_prompt", "")
        prompt = self.Job._get_prd_system_prompt()
        self.assertIsInstance(prompt, str)

    def test_qc_prompt_returns_custom_when_set(self):
        self._set_param("vegeta.qc_system_prompt", "CUSTOM QC")
        self.assertEqual(self.Job._get_qc_system_prompt(), "CUSTOM QC")

    def test_qc_prompt_falls_back_to_default(self):
        from ..services.qc_service import DEFAULT_QC_SYSTEM_PROMPT
        self._set_param("vegeta.qc_system_prompt", "")
        self.assertEqual(self.Job._get_qc_system_prompt(), DEFAULT_QC_SYSTEM_PROMPT)
