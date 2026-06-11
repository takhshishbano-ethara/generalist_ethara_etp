"""JD critique + rewrite pre-stage (P1-6): state machine, artifacts, linkage.

Contract under test (models/iris_job_description.py):

* ``draft → critiquing → critiqued → rewriting → rewritten → approved``
  with manager-only approve/reopen and deterministic failure reverts
  (critique → draft, rewrite → critiqued);
* one ``iris.jd.artifact`` row per LLM operation (parse-free mixin user:
  the markdown IS the artifact) with per-operation attempt counters and
  ``jd-{operation}-{slug}-{date}.md`` attachments;
* ``final_jd`` is seeded from the rewrite output and ``action_approve``
  hard-raises while any ``[FILL-IN`` placeholder remains;
* re-runs create NEW artifacts and never touch the prior run's audit;
* candidate linkage: an approved JD on ``candidate.jd_id`` appends the
  fenced ``ROLE CONTEXT — APPROVED JOB DESCRIPTION`` block to the
  screening prompt; without ``jd_id`` the block is absent entirely.
"""

import base64

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase, mock_llm


@tagged("post_install", "-at_install", "iris")
class TestJdFlow(IrisCase):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _critiqued_jd(self):
        jd = self._make_jd()
        jd.action_critique()
        with mock_llm(self.VALID_CRITIQUE_DOC):
            self._run_llm_queue()
        self.assertEqual(jd.state, "critiqued")
        return jd

    def _rewritten_jd(self):
        jd = self._critiqued_jd()
        jd.action_rewrite()
        with mock_llm(self.VALID_REWRITE_DOC):
            self._run_llm_queue()
        self.assertEqual(jd.state, "rewritten")
        return jd

    def _resolve_fillins(self, jd):
        jd.write({"final_jd": jd.final_jd.replace("[FILL-IN", "(resolved")})

    def _approved_jd(self):
        """A shortcut approved JD (state written directly) for linkage tests."""
        jd = self._make_jd()
        jd.write({
            "state": "approved",
            "final_jd": (
                "Own engineering end-to-end: the eval platform, the "
                "annotation platform, and the MLOps spine."
            ),
        })
        return jd

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------
    def test_happy_path_draft_to_approved(self):
        jd = self._make_jd()
        self.assertEqual(jd.state, "draft")

        critique = jd.action_critique()
        self.assertEqual(jd.state, "critiquing")
        self.assertEqual(critique.operation, "critique")
        self.assertEqual(critique.llm_status, "queued")

        with mock_llm(self.VALID_CRITIQUE_DOC):
            self._run_llm_queue()

        self.assertEqual(jd.state, "critiqued")
        self.assertEqual(critique.llm_status, "done")
        self.assertEqual(critique.markdown_result, self.VALID_CRITIQUE_DOC)
        self.assertEqual(jd.current_critique_id, critique)
        self.assertIn("Top 10 Key Insights", str(jd.critique_html))

        rewrite = jd.action_rewrite()
        self.assertEqual(jd.state, "rewriting")
        self.assertEqual(rewrite.operation, "rewrite")

        with mock_llm(self.VALID_REWRITE_DOC):
            self._run_llm_queue()

        self.assertEqual(jd.state, "rewritten")
        self.assertEqual(jd.current_rewrite_id, rewrite)
        # final_jd seeded verbatim from the rewrite output.
        self.assertEqual(jd.final_jd, self.VALID_REWRITE_DOC)
        self.assertTrue(jd.has_fillins)

        # Approval is hard-blocked while [FILL-IN placeholders remain.
        with self.assertRaises(UserError):
            jd.with_user(self.user_manager).action_approve()
        self.env.invalidate_all()
        self.assertEqual(jd.state, "rewritten")

        self._resolve_fillins(jd)
        self.assertFalse(jd.has_fillins)
        jd.with_user(self.user_manager).action_approve()
        self.assertEqual(jd.state, "approved")
        self.assertEqual(jd.approved_by, self.user_manager)
        self.assertTrue(jd.approved_at)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def test_critique_requires_jd_text(self):
        jd = self._make_jd(raw_jd=False)
        with self.assertRaises(UserError):
            jd.action_critique()
        self.env.invalidate_all()
        self.assertEqual(jd.state, "draft")
        self.assertFalse(jd.artifact_ids)

    def test_rewrite_blocked_without_critique(self):
        jd = self._make_jd()
        # Draft is not a rewrite-from state.
        with self.assertRaises(UserError):
            jd.action_rewrite()
        self.env.invalidate_all()
        self.assertFalse(jd.artifact_ids)

        # Right state but still no completed critique artifact.
        jd.write({"state": "critiqued"})
        with self.assertRaises(UserError):
            jd.action_rewrite()
        self.env.invalidate_all()
        self.assertFalse(jd.artifact_ids)

    def test_approve_requires_rewritten_state_and_final_jd(self):
        jd = self._critiqued_jd()
        with self.assertRaises(UserError):
            jd.with_user(self.user_manager).action_approve()
        self.env.invalidate_all()

        rewritten = self._rewritten_jd()
        rewritten.write({"final_jd": False})
        with self.assertRaises(UserError):
            rewritten.with_user(self.user_manager).action_approve()

    # ------------------------------------------------------------------
    # Failure reverts (+ retry restores the in-flight state)
    # ------------------------------------------------------------------
    def test_critique_failure_reverts_to_draft(self):
        jd = self._make_jd()
        artifact = jd.action_critique()
        with mock_llm(side_effect=Exception("critique boom")):
            self._run_llm_queue()

        self.assertEqual(artifact.llm_status, "failed")
        self.assertIn("critique boom", artifact.llm_error)
        self.assertEqual(jd.state, "draft")
        self.assertTrue(jd.last_llm_failed)
        self.assertIn("critique boom", jd.last_llm_error)

        # Retry re-enters critiquing and completes normally.
        artifact.action_retry_llm()
        self.assertEqual(jd.state, "critiquing")
        self.assertEqual(artifact.llm_status, "queued")
        with mock_llm(self.VALID_CRITIQUE_DOC):
            self._run_llm_queue()
        self.assertEqual(jd.state, "critiqued")
        self.assertEqual(artifact.llm_status, "done")

    def test_rewrite_failure_reverts_to_critiqued(self):
        jd = self._critiqued_jd()
        rewrite = jd.action_rewrite()
        with mock_llm(side_effect=Exception("rewrite boom")):
            self._run_llm_queue()

        self.assertEqual(rewrite.llm_status, "failed")
        self.assertEqual(jd.state, "critiqued")
        # final_jd untouched by the failure (never seeded).
        self.assertFalse(jd.final_jd)
        # The completed critique is still the current one.
        self.assertTrue(jd.current_critique_id)

    def test_retry_requires_failed_status(self):
        jd = self._critiqued_jd()
        with self.assertRaises(UserError):
            jd.artifact_ids.action_retry_llm()

    # ------------------------------------------------------------------
    # Rewrite prompt is derived from the critique
    # ------------------------------------------------------------------
    def test_rewrite_prompt_input_contains_critique_markdown(self):
        jd = self._critiqued_jd()
        rewrite = jd.action_rewrite()
        with mock_llm(self.VALID_REWRITE_DOC):
            self._run_llm_queue()

        prompt = rewrite.llm_prompt_input
        self.assertIn("ORIGINAL JOB DESCRIPTION:", prompt)
        self.assertIn("CRITIQUE DOCUMENT:", prompt)
        # The critique markdown itself rides into the rewrite call, fenced.
        self.assertIn("No compensation, equity, or funding disclosure", prompt)
        self.assertIn("BEGIN CRITIQUE DOCUMENT>>>", prompt)
        # The raw JD under review is fed alongside it.
        self.assertIn("visionary leader", prompt)

    # ------------------------------------------------------------------
    # FILL-IN approval guard
    # ------------------------------------------------------------------
    def test_fillin_guard_blocks_then_allows_after_edit(self):
        jd = self._rewritten_jd()
        self.assertIn("[FILL-IN", jd.final_jd)
        with self.assertRaises(UserError):
            jd.with_user(self.user_manager).action_approve()
        self.env.invalidate_all()

        # Even a single leftover placeholder still blocks.
        jd.write({"final_jd": "Almost done.\n[FILL-IN: comp band]"})
        with self.assertRaises(UserError):
            jd.with_user(self.user_manager).action_approve()
        self.env.invalidate_all()

        jd.write({
            "final_jd": "Compensation: INR 80L-1.1Cr + 0.3-0.6% equity.",
        })
        jd.with_user(self.user_manager).action_approve()
        self.assertEqual(jd.state, "approved")

    # ------------------------------------------------------------------
    # Manager-only approve / reopen
    # ------------------------------------------------------------------
    def test_approve_and_reopen_are_manager_only(self):
        jd = self._rewritten_jd()
        self._resolve_fillins(jd)

        with self.assertRaises(UserError):
            jd.with_user(self.user_iris).action_approve()
        self.env.invalidate_all()
        self.assertEqual(jd.state, "rewritten")

        jd.with_user(self.user_manager).action_approve()
        self.assertEqual(jd.state, "approved")

        with self.assertRaises(UserError):
            jd.with_user(self.user_iris).action_reopen()
        self.env.invalidate_all()
        self.assertEqual(jd.state, "approved")

        jd.with_user(self.user_manager).action_reopen()
        self.assertEqual(jd.state, "rewritten")
        self.assertFalse(jd.approved_by)
        self.assertFalse(jd.approved_at)

    def test_reopen_requires_approved_state(self):
        jd = self._rewritten_jd()
        with self.assertRaises(UserError):
            jd.with_user(self.user_manager).action_reopen()

    # ------------------------------------------------------------------
    # Per-operation attempt counters
    # ------------------------------------------------------------------
    def test_attempt_counters_are_per_operation(self):
        jd = self._critiqued_jd()
        first_critique = jd.artifact_ids
        self.assertEqual(first_critique.attempt, 1)

        jd.action_rewrite()
        with mock_llm(self.VALID_REWRITE_DOC):
            self._run_llm_queue()
        first_rewrite = jd.artifact_ids.filtered(
            lambda a: a.operation == "rewrite",
        )
        # The rewrite counter is independent of the critique's.
        self.assertEqual(first_rewrite.attempt, 1)

        # Re-critique (allowed from rewritten) → critique attempt #2.
        jd.action_critique()
        with mock_llm(self.VALID_CRITIQUE_DOC):
            self._run_llm_queue()
        critiques = jd.artifact_ids.filtered(
            lambda a: a.operation == "critique",
        ).sorted("id")
        self.assertEqual(critiques.mapped("attempt"), [1, 2])

        # Second rewrite → rewrite attempt #2 (critiques did not bump it).
        jd.action_rewrite()
        with mock_llm(self.VALID_REWRITE_DOC):
            self._run_llm_queue()
        rewrites = jd.artifact_ids.filtered(
            lambda a: a.operation == "rewrite",
        ).sorted("id")
        self.assertEqual(rewrites.mapped("attempt"), [1, 2])

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def test_artifact_attachment_names_and_content(self):
        jd = self._rewritten_jd()
        critique = jd.artifact_ids.filtered(
            lambda a: a.operation == "critique",
        )
        rewrite = jd.artifact_ids.filtered(
            lambda a: a.operation == "rewrite",
        )
        date_str = fields.Date.context_today(jd).isoformat()
        self.assertEqual(
            critique.attachment_id.name,
            f"jd-critique-head-of-engineering-{date_str}.md",
        )
        self.assertEqual(
            rewrite.attachment_id.name,
            f"jd-rewrite-head-of-engineering-{date_str}.md",
        )
        self.assertEqual(
            base64.b64decode(critique.attachment_id.datas).decode("utf-8"),
            self.VALID_CRITIQUE_DOC,
        )

    # ------------------------------------------------------------------
    # Re-runs preserve the prior artifact's audit
    # ------------------------------------------------------------------
    def test_recritique_creates_new_artifact_and_preserves_audit(self):
        jd = self._critiqued_jd()
        first = jd.artifact_ids
        first_raw = first.llm_raw_response
        first_attachment = first.attachment_id

        second_doc = self.VALID_CRITIQUE_DOC + "\n\nAddendum: second pass.\n"
        jd.action_critique()
        self.assertEqual(jd.state, "critiquing")
        with mock_llm(second_doc):
            self._run_llm_queue()

        artifacts = jd.artifact_ids.sorted("id")
        self.assertEqual(len(artifacts), 2)
        second = artifacts[-1]
        self.assertNotEqual(second, first)
        self.assertEqual(second.markdown_result, second_doc)

        # The first run's audit trail is byte-untouched.
        self.assertEqual(first.markdown_result, self.VALID_CRITIQUE_DOC)
        self.assertEqual(first.llm_status, "done")
        self.assertEqual(first.llm_raw_response, first_raw)
        self.assertTrue(first_attachment.exists())
        self.assertEqual(
            base64.b64decode(first_attachment.datas).decode("utf-8"),
            self.VALID_CRITIQUE_DOC,
        )

        # current_critique_id now points at the newest completed run.
        self.assertEqual(jd.current_critique_id, second)

    def test_second_rewrite_overwrites_edited_final_jd_with_note(self):
        jd = self._rewritten_jd()
        jd.write({"final_jd": "Manually edited final."})
        jd.action_rewrite()
        new_doc = self.VALID_REWRITE_DOC.replace(
            "Head of Engineering", "Head of Engineering v2",
        )
        with mock_llm(new_doc):
            self._run_llm_queue()

        self.assertEqual(jd.final_jd, new_doc)
        bodies = self._chatter_bodies(jd)
        self.assertTrue(
            any("overwritten by rewrite" in body for body in bodies),
            f"no overwrite note found in: {bodies}",
        )

    # ------------------------------------------------------------------
    # Candidate linkage → screening ROLE CONTEXT block
    # ------------------------------------------------------------------
    def test_candidate_jd_adds_role_context_to_screening_prompt(self):
        jd = self._approved_jd()
        candidate = self._make_candidate(jd_id=jd.id)
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)

        prompt = screening.llm_prompt_input
        self.assertIn("ROLE CONTEXT — APPROVED JOB DESCRIPTION:", prompt)
        # The JD text is fenced as untrusted data.
        self.assertIn("BEGIN APPROVED JOB DESCRIPTION>>>", prompt)
        self.assertIn("Own engineering end-to-end", prompt)
        # The rest of the INPUTS contract is intact around it.
        self.assertIn("CANDIDATE RESUME", prompt)

    def test_screening_prompt_without_jd_has_no_role_context(self):
        candidate = self._make_candidate()
        self.assertFalse(candidate.jd_id)
        screening = self._screen(candidate, self.VALID_SHIP_RECORD)

        prompt = screening.llm_prompt_input
        self.assertNotIn("ROLE CONTEXT", prompt)
        self.assertNotIn("APPROVED JOB DESCRIPTION", prompt)
