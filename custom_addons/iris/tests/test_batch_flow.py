"""Screening-batch lifecycle (v1.1).

Kickoff guards (member count, max-members ICP, draft+resume+role checks,
upfront API-key check), the N-screenings fan-out, mixed-verdict settlement
(SHIP / HOLD / BLOCK→pending_block all count as settled) auto-flipping the
batch to ``consistency``, the queue cron running the batch's own LLM job to
``done``, the report artifact (attachment naming + re-run revisions), the
failure→retry path, and the cost rollup.
"""

import base64

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import DEFAULT_LLM_RESULT, IrisCase, mock_llm


def _result(content):
    """A normalised llm_client result dict carrying ``content``."""
    return dict(DEFAULT_LLM_RESULT, content=content)


@tagged("post_install", "-at_install", "iris")
class TestBatchFlow(IrisCase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _settle_members(self, batch, contents):
        """Kick off + process each member screening with canned records.

        ``contents[i]`` answers the i-th screening in id order. Returns the
        member screenings sorted by id.
        """
        batch.action_screen_batch()
        with mock_llm(side_effect=[_result(content) for content in contents]):
            self._run_llm_queue()
        return batch.screening_ids.sorted("id")

    def _bound_report(self, batch):
        """VALID_BATCH_REPORT bound to the first two members' references."""
        members = batch.candidate_ids.sorted("id")
        return self.VALID_BATCH_REPORT.format(
            ref1=members[0].reference, ref2=members[1].reference,
        )

    def _run_consistency(self, batch, report):
        with mock_llm(report):
            self._run_llm_queue()
        return batch

    # ------------------------------------------------------------------
    # Kickoff guards
    # ------------------------------------------------------------------
    def test_kickoff_requires_draft_state(self):
        batch = self._make_batch()
        batch.write({"state": "cancelled"})
        with self.assertRaises(UserError):
            batch.action_screen_batch()

    def test_kickoff_requires_two_members(self):
        empty = self._make_batch(candidates=self.env["iris.candidate"])
        with self.assertRaises(UserError):
            empty.action_screen_batch()

        single = self._make_batch(candidates=[self._make_candidate(name="Solo")])
        with self.assertRaises(UserError):
            single.action_screen_batch()

    def test_kickoff_enforces_max_members_icp(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "iris.batch_max_members", "2",
        )
        members = (
            self._make_candidate(name="One")
            + self._make_candidate(name="Two")
            + self._make_candidate(name="Three")
        )
        batch = self._make_batch(candidates=members)
        with self.assertRaises(UserError) as ctx:
            batch.action_screen_batch()
        self.assertIn("capped", str(ctx.exception))
        self.assertEqual(batch.state, "draft")

        # Raising the cap unblocks the same batch.
        self.env["ir.config_parameter"].sudo().set_param(
            "iris.batch_max_members", "3",
        )
        batch.action_screen_batch()
        self.assertEqual(batch.state, "screening")

    def test_kickoff_requires_all_members_draft(self):
        batch = self._make_batch()
        batch.candidate_ids[0].write({"state": "shipped"})
        with self.assertRaises(UserError) as ctx:
            batch.action_screen_batch()
        self.assertIn("Draft", str(ctx.exception))

    def test_kickoff_requires_all_resumes(self):
        batch = self._make_batch()
        batch.candidate_ids[0].write({"resume_text": False})
        with self.assertRaises(UserError) as ctx:
            batch.action_screen_batch()
        self.assertIn("resume", str(ctx.exception))

    def test_kickoff_requires_role_match(self):
        other_role = self._make_role(name="Mismatch Role")
        mismatched = self._make_candidate(
            name="Wrong Role", role_id=other_role.id,
        )
        batch = self._make_batch(candidates=[
            self._make_candidate(name="Right Role"), mismatched,
        ])
        with self.assertRaises(UserError) as ctx:
            batch.action_screen_batch()
        self.assertIn("Wrong Role", str(ctx.exception))

    def test_kickoff_requires_api_key_upfront(self):
        batch = self._make_batch()
        self._clear_api_key()
        with self.assertRaises(UserError) as ctx:
            batch.action_screen_batch()
        self.assertIn("API key", str(ctx.exception))
        # Fail-fast: nothing was touched.
        self.assertEqual(batch.state, "draft")
        self.assertFalse(batch.screening_ids)
        self.assertEqual(set(batch.candidate_ids.mapped("state")), {"draft"})

    def test_role_locked_once_screening_started(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        other_role = self._make_role(name="Late Role Change")
        with self.assertRaises(UserError):
            batch.write({"role_id": other_role.id})

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------
    def test_kickoff_enqueues_one_screening_per_member(self):
        members = (
            self._make_candidate(name="One")
            + self._make_candidate(name="Two")
            + self._make_candidate(name="Three")
        )
        batch = self._make_batch(candidates=members)
        batch.action_screen_batch()

        self.assertEqual(batch.state, "screening")
        self.assertEqual(len(batch.screening_ids), 3)
        self.assertEqual(set(batch.screening_ids.mapped("llm_status")), {"queued"})
        self.assertEqual(set(members.mapped("state")), {"screening"})
        for member in members:
            self.assertEqual(len(member.screening_ids), 1)
        self.assertTrue(any(
            "Batch screening started" in body
            for body in self._chatter_bodies(batch)
        ))

    # ------------------------------------------------------------------
    # Mixed settlement → consistency → done
    # ------------------------------------------------------------------
    def test_mixed_verdicts_settle_to_done(self):
        members = (
            self._make_candidate(name="Alpha One")
            + self._make_candidate(name="Beta Two")
            + self._make_candidate(name="Gamma Three")
        )
        batch = self._make_batch(candidates=members)
        screenings = self._settle_members(batch, [
            self.VALID_SHIP_RECORD,
            self.VALID_HOLD_RECORD,
            self.VALID_BLOCK_RECORD,
        ])

        self.assertEqual(screenings.mapped("verdict"), ["ship", "hold", "block"])
        self.assertEqual(screenings[0].candidate_id.state, "shipped")
        self.assertEqual(screenings[1].candidate_id.state, "hold")
        # BLOCK lands as pending_block (dual sign-off) — still SETTLED.
        self.assertEqual(screenings[2].candidate_id.state, "pending_block")

        self.assertEqual(batch.member_count, 3)
        self.assertEqual(batch.settled_count, 3)
        self.assertFalse(batch.blocking_candidate_ids)
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")

        report = self._bound_report(batch)
        self._run_consistency(batch, report)

        self.assertEqual(batch.state, "done")
        self.assertEqual(batch.llm_status, "done")
        self.assertEqual(batch.batch_report_markdown, report)

        prompt = batch.llm_prompt_input
        self.assertIn("MEMBER COUNT:        3", prompt)
        self.assertIn("ROLE / LEVEL:        Head of Engineering", prompt)
        self.assertIn("===== CANDIDATE 1/3:", prompt)
        self.assertIn("===== END CANDIDATE 3/3 =====", prompt)
        # Member records enter the consistency prompt fenced as untrusted.
        self.assertIn("<<<IRIS-DATA-", prompt)
        # The pending sign-off is annotated per the prompt contract.
        self.assertIn("BLOCK (pending second sign-off)", prompt)

    # ------------------------------------------------------------------
    # Report artifact
    # ------------------------------------------------------------------
    def test_report_attachment_naming_and_rerun_revision(self):
        batch = self._make_batch()
        self._settle_members(
            batch, [self.VALID_SHIP_RECORD, self.VALID_SHIP_RECORD],
        )
        report = self._bound_report(batch)
        self._run_consistency(batch, report)

        attachment = batch.report_attachment_id
        self.assertTrue(attachment)
        date_str = fields.Date.context_today(batch).isoformat()
        self.assertEqual(
            attachment.name, f"batch-report-{batch.name}-{date_str}.md",
        )
        self.assertEqual(attachment.res_model, "iris.screening.batch")
        self.assertEqual(attachment.res_id, batch.id)
        self.assertEqual(
            base64.b64decode(attachment.datas).decode("utf-8"), report,
        )

        # Manager re-run: the prior report is preserved as a revision in the
        # SAME attachment, and the chatter records the prior run's cost.
        batch.action_rerun_consistency()
        self.assertEqual(batch.state, "consistency")
        self.assertTrue(any(
            "Re-running consistency review" in body
            for body in self._chatter_bodies(batch)
        ))
        self._run_consistency(batch, report)
        self.assertEqual(batch.state, "done")
        self.assertEqual(batch.report_attachment_id, attachment)
        content = base64.b64decode(attachment.datas).decode("utf-8")
        self.assertIn("# Revision —", content)
        self.assertEqual(
            content.count("# Batch Screening Consistency Report"), 2,
        )

    def test_rerun_guards(self):
        batch = self._make_batch()
        with self.assertRaises(UserError):
            batch.action_rerun_consistency()  # only from done

        self._settle_members(
            batch, [self.VALID_SHIP_RECORD, self.VALID_SHIP_RECORD],
        )
        self._run_consistency(batch, self._bound_report(batch))
        self.assertEqual(batch.state, "done")
        with self.assertRaises(UserError):
            batch.with_user(self.user_iris).action_rerun_consistency()

    def test_manual_consistency_trigger_requires_settled_members(self):
        batch = self._make_batch()
        batch.action_screen_batch()
        with self.assertRaises(UserError) as ctx:
            batch.action_run_consistency()
        self.assertIn("settled", str(ctx.exception))

    # ------------------------------------------------------------------
    # Failure → retry
    # ------------------------------------------------------------------
    def test_consistency_failure_then_retry(self):
        batch = self._make_batch()
        self._settle_members(
            batch, [self.VALID_SHIP_RECORD, self.VALID_SHIP_RECORD],
        )
        self.assertEqual(batch.state, "consistency")

        with mock_llm(side_effect=Exception("gateway down")):
            self._run_llm_queue()
        self.assertEqual(batch.state, "failed")
        self.assertEqual(batch.llm_status, "failed")
        # Members are untouched by a consistency failure.
        self.assertEqual(set(batch.candidate_ids.mapped("state")), {"shipped"})

        batch.action_retry_llm()
        self.assertEqual(batch.state, "consistency")
        self.assertEqual(batch.llm_status, "queued")
        self._run_consistency(batch, self._bound_report(batch))
        self.assertEqual(batch.state, "done")

    def test_retry_guarded_to_failed_runs(self):
        batch = self._make_batch()
        with self.assertRaises(UserError):
            batch.action_retry_llm()

    # ------------------------------------------------------------------
    # Cost rollup
    # ------------------------------------------------------------------
    def test_total_cost_rolls_up_members_plus_consistency(self):
        batch = self._make_batch()
        self._settle_members(
            batch, [self.VALID_SHIP_RECORD, self.VALID_HOLD_RECORD],
        )
        # 2 member screenings × $0.01, consistency not yet run.
        self.assertAlmostEqual(batch.total_cost_usd, 0.02, places=6)

        self._run_consistency(batch, self._bound_report(batch))
        # + the consistency pass's own $0.01.
        self.assertAlmostEqual(batch.total_cost_usd, 0.03, places=6)
