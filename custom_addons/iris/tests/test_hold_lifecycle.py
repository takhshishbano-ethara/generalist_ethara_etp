"""HOLD lifecycle: deadline math, evidence gating, re-screen chain, auto-block.

The HOLD episode contract (plan + model docstrings):

* ``hold_deadline`` = ``add_business_days(today, 5)`` set on the FIRST hold;
* a re-HOLD keeps the ORIGINAL deadline (never extended);
* ship/block clears the deadline;
* the daily cron blocks holds strictly past the deadline (``<`` today) and
  flags the hold screening ``auto_blocked``.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm
from odoo.addons.iris.services.business_days import add_business_days

EVIDENCE = "Reference call with the former manager confirms the 40M/day claim."


@tagged("post_install", "-at_install", "iris")
class TestHoldLifecycle(IrisCase):
    def _hold_candidate(self, name="Jane Doe"):
        candidate = self._make_candidate(name=name)
        self._screen(candidate, self.VALID_HOLD_RECORD)
        self.assertEqual(candidate.state, "hold")
        return candidate

    def _record_evidence(self, candidate, rescreen_now=False):
        wizard = self.env["iris.evidence.wizard"].create({
            "candidate_id": candidate.id,
            "evidence": EVIDENCE,
            "rescreen_now": rescreen_now,
        })
        wizard.action_confirm()
        return wizard

    # ------------------------------------------------------------------
    # Deadline math
    # ------------------------------------------------------------------
    def test_deadline_is_five_business_days_from_today(self):
        candidate = self._hold_candidate()
        today = fields.Date.context_today(candidate)
        self.assertEqual(candidate.hold_deadline, add_business_days(today, 5))

    def test_deadline_honours_configured_business_days(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "iris.hold_business_days", "10",
        )
        candidate = self._hold_candidate()
        today = fields.Date.context_today(candidate)
        self.assertEqual(candidate.hold_deadline, add_business_days(today, 10))

    # ------------------------------------------------------------------
    # Evidence gating
    # ------------------------------------------------------------------
    def test_rescreen_without_evidence_raises(self):
        candidate = self._hold_candidate()
        with self.assertRaises(UserError):
            candidate.action_rescreen()
        self.env.invalidate_all()
        self.assertEqual(candidate.state, "hold")
        self.assertEqual(len(candidate.screening_ids), 1)

    def test_rescreen_requires_hold_state(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        with self.assertRaises(UserError):
            candidate.action_rescreen()

    def test_evidence_wizard_requires_hold_state(self):
        candidate = self._make_candidate()
        wizard = self.env["iris.evidence.wizard"].create({
            "candidate_id": candidate.id,
            "evidence": EVIDENCE,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    # ------------------------------------------------------------------
    # Evidence wizard + re-screen chain
    # ------------------------------------------------------------------
    def test_evidence_wizard_writes_fields_and_rescreens(self):
        candidate = self._hold_candidate()
        hold_screening = candidate._get_current_hold_screening()
        self._record_evidence(candidate, rescreen_now=True)

        self.assertEqual(hold_screening.verification_evidence, EVIDENCE)
        self.assertEqual(hold_screening.evidence_recorded_by, self.env.user)
        self.assertTrue(hold_screening.evidence_recorded_at)

        rescreen = candidate.screening_ids.sorted("id")[-1]
        self.assertNotEqual(rescreen, hold_screening)
        self.assertTrue(rescreen.is_rescreen)
        self.assertEqual(rescreen.parent_screening_id, hold_screening)
        self.assertEqual(rescreen.llm_status, "queued")
        self.assertEqual(rescreen.attempt, 2)
        self.assertEqual(candidate.state, "screening")

    def test_evidence_without_rescreen_keeps_hold(self):
        candidate = self._hold_candidate()
        self._record_evidence(candidate, rescreen_now=False)
        self.assertEqual(candidate.state, "hold")
        self.assertEqual(len(candidate.screening_ids), 1)

    def test_rescreen_prompt_includes_evidence_and_prior_record(self):
        candidate = self._hold_candidate()
        self._record_evidence(candidate, rescreen_now=True)
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        rescreen = candidate.screening_ids.sorted("id")[-1]
        self.assertIn("VERIFICATION EVIDENCE", rescreen.llm_prompt_input)
        self.assertIn(EVIDENCE, rescreen.llm_prompt_input)
        self.assertIn("PRIOR HOLD RECORD", rescreen.llm_prompt_input)

    def test_rescreen_failure_reverts_to_hold(self):
        candidate = self._hold_candidate()
        self._record_evidence(candidate, rescreen_now=True)
        with mock_llm(side_effect=Exception("boom")):
            self._run_llm_queue()
        rescreen = candidate.screening_ids.sorted("id")[-1]
        self.assertEqual(rescreen.llm_status, "failed")
        self.assertEqual(candidate.state, "hold")

    # ------------------------------------------------------------------
    # Re-HOLD keeps the original deadline; ship/block clears it
    # ------------------------------------------------------------------
    def test_rehold_keeps_original_deadline(self):
        candidate = self._hold_candidate()
        sentinel = fields.Date.context_today(candidate) + timedelta(days=1)
        candidate.write({"hold_deadline": sentinel})

        self._record_evidence(candidate, rescreen_now=True)
        with mock_llm(self.VALID_HOLD_RECORD):
            self._run_llm_queue()

        self.assertEqual(candidate.state, "hold")
        self.assertEqual(candidate.hold_deadline, sentinel,
                         "re-HOLD must never extend the original deadline")
        rescreen = candidate.screening_ids.sorted("id")[-1]
        self.assertEqual(rescreen.verdict, "hold")
        self.assertEqual(rescreen.hold_deadline, sentinel)

    def test_rescreen_ship_clears_deadline(self):
        candidate = self._hold_candidate()
        self._record_evidence(candidate, rescreen_now=True)
        with mock_llm(self.VALID_SHIP_RECORD):
            self._run_llm_queue()
        self.assertEqual(candidate.state, "shipped")
        self.assertFalse(candidate.hold_deadline)

    # ------------------------------------------------------------------
    # Auto-block cron
    # ------------------------------------------------------------------
    def test_cron_blocks_only_past_deadline(self):
        expired = self._hold_candidate(name="Eve Expired")
        boundary = self._hold_candidate(name="Bob Boundary")
        future = self._hold_candidate(name="Fred Future")

        today = fields.Date.context_today(expired)
        expired.write({"hold_deadline": today - timedelta(days=1)})
        boundary.write({"hold_deadline": today})
        future.write({"hold_deadline": today + timedelta(days=3)})
        self.env.flush_all()

        self.env["iris.candidate"]._cron_auto_block_expired_holds()

        self.assertEqual(expired.state, "blocked")
        self.assertFalse(expired.hold_deadline)
        self.assertTrue(expired.screening_ids.sorted("id")[-1].auto_blocked)

        # Strict < today: a deadline of exactly today is NOT blocked yet.
        self.assertEqual(boundary.state, "hold")
        self.assertFalse(boundary.screening_ids.mapped("auto_blocked")[0])
        self.assertEqual(future.state, "hold")

    def test_cron_posts_auto_block_chatter(self):
        candidate = self._hold_candidate()
        yesterday = fields.Date.context_today(candidate) - timedelta(days=1)
        candidate.write({"hold_deadline": yesterday})
        self.env.flush_all()

        self.env["iris.candidate"]._cron_auto_block_expired_holds()

        bodies = self._chatter_bodies(candidate)
        self.assertTrue(
            any("auto-blocked" in body for body in bodies),
            f"no auto-block chatter message found in: {bodies}",
        )

    def test_cron_ignores_non_hold_states(self):
        candidate = self._make_candidate()
        self._screen(candidate, self.VALID_SHIP_RECORD)
        yesterday = fields.Date.context_today(candidate) - timedelta(days=1)
        candidate.write({"hold_deadline": yesterday})  # inconsistent on purpose
        self.env.flush_all()
        self.env["iris.candidate"]._cron_auto_block_expired_holds()
        self.assertEqual(candidate.state, "shipped")
