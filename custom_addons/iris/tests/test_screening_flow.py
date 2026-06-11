"""Screening queue mechanics: cron processing, attachments, metadata, reaper."""

import base64

from odoo import fields
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm


@tagged("post_install", "-at_install", "iris")
class TestScreeningFlow(IrisCase):
    def test_enqueue_then_cron_processes_to_done(self):
        candidate = self._make_candidate()
        candidate.action_screen()
        screening = candidate.screening_ids
        self.assertEqual(screening.llm_status, "queued")
        self.assertFalse(screening.markdown_record)

        with mock_llm(self.VALID_SHIP_RECORD) as mocked:
            self._run_llm_queue()

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(screening.llm_status, "done")
        self.assertEqual(screening.markdown_record, self.VALID_SHIP_RECORD)
        self.assertTrue(screening.screened_at)

    def test_enqueue_creates_instant_cron_trigger(self):
        cron = self.env.ref("iris.cron_process_llm_queue")
        before = self.env["ir.cron.trigger"].search_count([
            ("cron_id", "=", cron.id),
        ])
        candidate = self._make_candidate()
        candidate.action_screen()
        after = self.env["ir.cron.trigger"].search_count([
            ("cron_id", "=", cron.id),
        ])
        self.assertEqual(after, before + 1)

    def test_attachment_naming_and_content(self):
        candidate = self._make_candidate(name="Jane Doe")
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)

        attachment = screening.attachment_id
        self.assertTrue(attachment)
        date_str = fields.Date.context_today(screening).isoformat()
        self.assertEqual(attachment.name, f"screening-doe-{date_str}.md")
        self.assertEqual(attachment.res_model, "iris.screening")
        self.assertEqual(attachment.res_id, screening.id)
        self.assertEqual(attachment.mimetype, "text/markdown")
        self.assertEqual(
            base64.b64decode(attachment.datas).decode("utf-8"),
            self.VALID_SHIP_RECORD,
        )

    def test_attachment_lastname_is_last_token_lowercased(self):
        candidate = self._make_candidate(name="Ada Augusta KING-Lovelace")
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        date_str = fields.Date.context_today(screening).isoformat()
        self.assertEqual(
            screening.attachment_id.name,
            f"screening-king-lovelace-{date_str}.md",
        )

    def test_llm_cost_and_token_metadata_stored(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)

        self.assertEqual(screening.llm_prompt_tokens, 100)
        self.assertEqual(screening.llm_completion_tokens, 200)
        self.assertAlmostEqual(screening.llm_cost_usd, 0.01)
        self.assertEqual(screening.llm_model_used, "test")
        self.assertEqual(screening.llm_latency_ms, 50)
        self.assertTrue(screening.llm_started_at)
        self.assertTrue(screening.llm_completed_at)
        self.assertEqual(screening.llm_raw_response, "{}")

    def test_cost_fallback_from_tokens_when_gateway_omits_cost(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "iris.usd_per_mtoken", "4.0",
        )
        candidate = self._make_candidate()
        candidate.action_screen()
        with mock_llm(self.VALID_SHIP_RECORD, cost_usd=None):
            self._run_llm_queue()
        screening = candidate.screening_ids
        # (100 + 200) tokens / 1M * $4 = $0.0012
        self.assertAlmostEqual(screening.llm_cost_usd, 0.0012)

    def test_prompt_input_audited(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        self.assertIn("INPUTS:", screening.llm_prompt_input)
        self.assertIn("Senior ML Engineer", screening.llm_prompt_input)
        self.assertIn(self.RESUME_TEXT, screening.llm_prompt_input)

    def test_markdown_html_rendered(self):
        candidate = self._make_candidate()
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)
        html = str(screening.markdown_html)
        self.assertIn("<h1>", html)
        self.assertIn("Screening Record", html)

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------
    def test_reaper_marks_stale_running_as_failed(self):
        candidate = self._make_candidate()
        candidate.write({"state": "screening"})
        screening = self.env["iris.screening"].create({
            "candidate_id": candidate.id,
        })
        screening.write({
            "llm_status": "running",
            "llm_started_at": fields.Datetime.subtract(
                fields.Datetime.now(), minutes=40,
            ),
        })
        self.env.flush_all()

        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()

        self.assertEqual(screening.llm_status, "failed")
        self.assertIn("did not complete", screening.llm_error)
        # Failure path reverts the candidate deterministically.
        self.assertEqual(candidate.state, "draft")

    def test_reaper_leaves_fresh_running_jobs_alone(self):
        candidate = self._make_candidate()
        candidate.write({"state": "screening"})
        screening = self.env["iris.screening"].create({
            "candidate_id": candidate.id,
        })
        screening.write({
            "llm_status": "running",
            "llm_started_at": fields.Datetime.subtract(
                fields.Datetime.now(), minutes=5,
            ),
        })
        self.env.flush_all()

        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()

        self.assertEqual(screening.llm_status, "running")
        self.assertEqual(candidate.state, "screening")

    def test_queue_processes_multiple_models_in_one_sweep(self):
        # A queued interview and a queued screening both complete in one run.
        guide_candidate = self._make_candidate(name="Gina Guide")
        self._screen(guide_candidate, self.VALID_SHIP_RECORD)
        guide_candidate.action_generate_guide()

        screen_candidate = self._make_candidate(name="Sara Screen")
        screen_candidate.action_screen()

        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()

        self.assertEqual(screen_candidate.screening_ids.llm_status, "done")
        self.assertEqual(guide_candidate.interview_ids.llm_status, "done")
