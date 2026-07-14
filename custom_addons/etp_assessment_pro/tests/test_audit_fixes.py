# -*- coding: utf-8 -*-
"""Regression tests for the 2026-07-02 production-readiness audit fixes.

Locks in the behaviours that had NO coverage before (the audit specifically
noted the suite 'misses the evaluator error-rollup path where the P0 lives'):
  * P0-1 — evaluator subjective rollup writing llm_state='error' must NOT crash.
  * P0-1 (full path) — scoring while Vertex is unavailable degrades to a clean
    'error'/'failed' with the immutable raw score untouched (no silent 0).
  * Reset & re-score recovery action re-queues errored answers.
  * P1-5 — an image_label answer with gate dimensions but no pick is dropped in
    _record_response (was: uncaught UserError -> raw 500 page mid-exam).
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.etp_assessment_pro.tests.test_phase23_lifecycle import _Base
from odoo.addons.etp_assessment_pro.services import vertex as vertex_svc
from odoo.addons.etp_assessment_pro.controllers import portal as portal_ctrl


class _FakeRequest:
    def __init__(self, env):
        self.env = env


class TestAuditFixes(_Base):

    def _subjective_ctx(self, justification="Answer text"):
        cat = self._make_category()
        q = self._make_subjective("AF-SUBJ", category=cat)
        emp = self._make_applicant("AFCand")
        a = self.Assessment.create({
            "name": "AF-A",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q.id])]})
        ev = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": emp.id,
            "question_order": json.dumps([q.id]), "total_questions": 1,
            "state": "submitted",
        })
        r = self.Response.create({
            "assessment_id": a.id, "assessment_evaluator_id": ev.id,
            "evaluator_id": emp.id, "question_id": q.id,
            "justification": justification, "state": "submitted",
        })
        return a, ev, q, r

    def test_p0_error_rollup_does_not_crash(self):
        _a, ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "error"})          # needs_llm answer terminal-errored
        ev.invalidate_recordset()
        ev._compute_subjective_rollup()          # pre-fix: ValueError
        ev.invalidate_recordset()
        self.assertEqual(ev.llm_state, "error")

    def test_p0_scoring_when_vertex_down_is_graceful(self):
        _a, ev, _q, r = self._subjective_ctx()

        def boom(*args, **kwargs):
            raise ValueError("Vertex/Gemini not configured ...")

        with patch.object(vertex_svc, "_call_vertex", side_effect=boom):
            for _ in range(3):                   # exhaust retries -> terminal error
                ev.action_llm_score()            # must not raise
        r.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertIn(r.llm_state, ("error", "failed"))
        self.assertEqual(r.llm_raw_100, 0.0)     # never silently scored
        self.assertIn(ev.llm_state, ("error", "failed", "partial"))

    def test_reset_errored_scoring_requeues(self):
        _a, ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "error", "llm_attempts": 3})
        ev.action_reset_errored_scoring()
        r.invalidate_recordset()
        ev.invalidate_recordset()
        self.assertEqual(r.llm_state, "pending")
        self.assertEqual(r.llm_attempts, 0)
        self.assertTrue(ev.scoring_requested)

    def test_p1_5_image_label_gate_without_pick_is_dropped(self):
        cat = self._make_category()
        q = self.Question.create({
            "name": "AF-IMGLBL", "question_type": "image_label",
            "prompt": "Label the image", "difficulty": "easy",
            "generator_id": cat.id,
        })
        self.QDim.create({
            "question_id": q.id,
            "name": "Gate",
            "option_line_ids": [
                (0, 0, {"name": "Yes", "sequence": 10}),
                (0, 0, {"name": "No", "sequence": 20}),
            ],
        })
        emp = self._make_applicant("AF-IT")
        a = self.Assessment.create({
            "name": "AF-IT-A",
            "generator_id": cat.id, "question_limit": 1, "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q.id])]})
        ev = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": emp.id,
            "question_order": json.dumps([q.id]), "total_questions": 1,
        })
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            res = ctrl._record_response(
                ev,
                {"question_id": str(q.id), "justification": "my text answer"})
        self.assertFalse(
            res, "image_label with gate dims but no pick must not submit (P1-5)")
        # C2: the typed justification is preserved as a DRAFT (survives back-nav),
        # not silently dropped; the question stays unanswered (state != submitted).
        rows = self.Response.search([("assessment_evaluator_id", "=", ev.id)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.state, "draft")
        self.assertEqual(rows.justification, "my text answer")

    def test_threshold_change_after_done_flips_overall_result(self):
        """bp-1: the per-assessment subjective_threshold is editable after the
        assessment is done and re-decides Pass/Fail live (no LLM re-run)."""
        a, ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "scored", "llm_raw_100": 75.0})
        a.subjective_threshold = 70.0
        ev.invalidate_recordset()
        r.invalidate_recordset()
        self.assertEqual(r.llm_score, 1)          # 75 >= 70
        self.assertEqual(ev.result, "pass")       # score% 100 >= 70
        # Editable even once the assessment is marked done; raising it flips the
        # candidate's result with no re-scoring.
        a.write({"state": "done"})
        a.subjective_threshold = 80.0
        ev.invalidate_recordset()
        r.invalidate_recordset()
        self.assertEqual(r.llm_score, 0)          # 75 < 80
        self.assertEqual(ev.result, "fail")
        self.assertAlmostEqual(r.llm_raw_100, 75.0)   # raw untouched

    def test_errored_subjective_holds_result_pending(self):
        """P1 (audit 2): a subjective answer terminal-errored by a Vertex outage
        must keep the candidate 'pending' (needs admin Reset & Re-score), NOT
        silently finalize as a scored 0 and (mis)fail the candidate."""
        _a, ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "error"})
        ev.invalidate_recordset()
        self.assertTrue(ev.subjective_pending)   # errored counts as unresolved
        self.assertEqual(ev.result, "pending")   # not finalized to pass/fail

    def test_scoring_error_flag_marks_errored_candidate(self):
        """A candidate with a terminal-errored subjective answer shows the '!'
        flag so the admin can spot it in the candidate list."""
        _a, ev, _q, r = self._subjective_ctx()
        self.assertFalse(ev.scoring_error_flag)   # clean -> no flag
        r.write({"llm_state": "error"})
        ev.invalidate_recordset()
        self.assertEqual(ev.scoring_error_flag, "!")

    def test_zero_threshold_is_honored_not_coerced_to_70(self):
        """Verify-pass fix: a deliberately-set 0%% pass bar must stay 0, not be
        coerced to 70 by a truthiness (`or 70.0`) check."""
        a, ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "scored", "llm_raw_100": 40.0})
        a.subjective_threshold = 0.0
        ev.invalidate_recordset()
        r.invalidate_recordset()
        self.assertEqual(r.llm_score, 1)          # 40 >= 0 (not < 70)
        self.assertEqual(ev.pass_threshold, 0.0)  # stored bar matches input

    def test_record_response_rejects_question_not_in_order(self):
        """Audit P1: a candidate cannot inject a response for a question outside
        their assigned question_order (score inflation)."""
        _a, ev, _q, _r = self._subjective_ctx()
        foreign = self._make_subjective("AF-FOREIGN")
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            res = ctrl._record_response(
                ev,
                {"question_id": str(foreign.id), "justification": "sneaky"})
        self.assertFalse(res)
        self.assertFalse(self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", foreign.id)]))

    def test_threshold_recompute_cron_applies_deferred_change(self):
        """Audit P1 / Option A: the deferred (large-assessment) path — a threshold
        change flagged for the cron is applied by _cron_recompute_subjective_results,
        which then clears the flag."""
        a, _ev, _q, r = self._subjective_ctx()
        r.write({"llm_state": "scored", "llm_raw_100": 75.0})
        self.assertEqual(r.llm_score, 1)          # 75 >= default 70
        # Simulate the large-assessment branch: set threshold + flag via raw SQL
        # so the inline write() recompute does NOT run.
        self.env.cr.execute(
            "UPDATE etp_assessment_pro SET subjective_threshold = 80.0, "
            "threshold_recompute_pending = TRUE WHERE id = %s", (a.id,))
        a.invalidate_recordset()
        with patch.object(self.env.cr, "commit"):
            self.Assessment._cron_recompute_subjective_results()
        a.invalidate_recordset()
        r.invalidate_recordset()
        self.assertFalse(a.threshold_recompute_pending)
        self.assertEqual(r.llm_score, 0)          # cron applied 75 < 80


class TestCandidateInvite(_Base):
    """Password/invite fix: a fresh candidate gets a signup token; an email that
    matches an INTERNAL account is never bound/reactivated (F3)."""

    def test_fresh_candidate_provisioned_then_invited_by_cron(self):
        a = self.Assessment.create({"name": "InviteA", "generator_id": self._make_category().id})
        app = self.Applicant.create({
            "partner_name": "Fresh Cand", "email_from": "freshcand@x.com"})
        # Provisioning creates the portal user but NO LONGER sends the invite
        # synchronously — that is deferred to the background invite cron.
        self.assertEqual(a._ensure_candidate_user(app), "created")
        app.invalidate_recordset()
        user = app.candidate_user_id
        self.assertTrue(user and user.has_group("base.group_portal"))
        self.assertFalse(user.partner_id.sudo().signup_type)   # not invited yet
        # Delivery (what the cron calls) prepares the 6-day signup token; mock
        # the actual SMTP send so the test doesn't hit a mail server.
        ev = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": app.id})
        with patch("odoo.addons.mail.models.mail_template."
                   "MailTemplate.send_mail"):
            ev._deliver_invitation()
        user.partner_id.invalidate_recordset()
        self.assertTrue(user.partner_id.sudo().signup_type)

    def test_internal_user_email_not_bound(self):
        internal = self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Staff", "login": "staff_collide@x.com",
                "email": "staff_collide@x.com",
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        a = self.Assessment.create({"name": "InviteB", "generator_id": self._make_category().id})
        app = self.Applicant.create({
            "partner_name": "Collide", "email_from": "staff_collide@x.com"})
        self.assertEqual(a._ensure_candidate_user(app), "skipped")
        app.invalidate_recordset()
        self.assertFalse(app.candidate_user_id)   # not bound to the internal user
        self.assertTrue(internal.active)          # and left untouched

    def test_internal_user_resolved_as_own_candidate(self):
        """Internal user, never bound as candidate_user_id, is still resolved as
        their own candidate via login==email so the exam guard/portal accept
        them (companion to test_internal_user_email_not_bound)."""
        internal = self.env["res.users"].with_context(
            no_reset_password=True).create({
                "name": "Staff2", "login": "staff_cand@x.com",
                "email": "staff_cand@x.com",
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        a = self.Assessment.create({"name": "InviteC", "generator_id": self._make_category().id})
        app = self.Applicant.create({
            "partner_name": "StaffCand", "email_from": "staff_cand@x.com"})
        self.assertEqual(a._ensure_candidate_user(app), "skipped")
        app.invalidate_recordset()
        self.assertFalse(app.candidate_user_id)
        ev = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": app.id})
        self.assertTrue(internal._is_internal())
        self.assertEqual(ev._candidate_user(), internal)
        other = self.Applicant.create({
            "partner_name": "Other", "email_from": "someoneelse@x.com"})
        ev2 = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": other.id})
        self.assertNotEqual(ev2._candidate_user(), internal)

    def test_deactivated_portal_user_not_reactivated(self):
        """Audit 7: re-adding a candidate whose email matches a DEACTIVATED
        portal account links to it but must NOT silently reactivate it (an admin
        may have disabled it on purpose)."""
        portal = self.env.ref("base.group_portal")
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "OldCand", "login": "old_cand@x.com",
            "email": "old_cand@x.com", "group_ids": [(6, 0, [portal.id])]})
        user.active = False
        a = self.Assessment.create({"name": "ReactA", "generator_id": self._make_category().id})
        app = self.Applicant.create({
            "partner_name": "OldCand", "email_from": "old_cand@x.com"})
        self.assertEqual(a._ensure_candidate_user(app), "linked")
        user.invalidate_recordset()
        self.assertFalse(user.active)             # NOT silently reactivated
        app.invalidate_recordset()
        self.assertEqual(app.candidate_user_id, user)   # still linked


class TestClientPooling(_Base):
    """SVC-5: the Vertex HTTP client and the S3 client are pooled/cached, not
    rebuilt on every call (connection reuse under 200-300 candidate load)."""

    def test_vertex_httpx_client_is_pooled(self):
        self.assertIs(vertex_svc._httpx(), vertex_svc._httpx())

    def test_s3_client_is_cached_per_creds(self):
        from odoo.addons.etp_assessment_pro.services import s3_service
        self.assertIs(s3_service._client(self.env), s3_service._client(self.env))


class TestConcurrencyGuards(_Base):
    """Audit 3: a DB unique index backstops the duplicate-evaluator race."""

    def _assessment(self):
        cat = self._make_category()
        a = self.Assessment.create({
            "name": "MDGuard", "generator_id": cat.id})
        app = self.Applicant.create({
            "partner_name": "MDG", "email_from": "mdg@x.com"})
        ev = self.Evaluator.create({
            "assessment_id": a.id, "applicant_id": app.id})
        return a, app, ev

    def test_duplicate_evaluator_blocked(self):
        from psycopg2 import IntegrityError
        a, app, _ev = self._assessment()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.Evaluator.create({
                "assessment_id": a.id, "applicant_id": app.id})


class TestInviteQueue(_Base):
    """Batched background invitations: launch QUEUES (never blasts SMTP in the
    request), the cron sends in batches, and a failed send flags 'failed'."""

    def _launchable(self, n=3):
        cat = self._make_category()
        self._make_mcq("IQ-MCQ", category=cat)
        apps = [self.Applicant.create({
            "partner_name": "IQ%d" % i, "email_from": "iq%d@x.com" % i})
            for i in range(n)]
        a = self.Assessment.create({
            "name": "InvQ", "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [x.id for x in apps])]})
        return a

    def test_launch_queues_invites_not_synchronous(self):
        a = self._launchable(3)
        a.action_start()                  # must NOT block on SMTP / raise
        evs = a.assessment_evaluator_ids
        self.assertEqual(len(evs), 3)
        self.assertTrue(all(e.invite_state == "queued" for e in evs))
        a.invalidate_recordset()
        self.assertIn("3 queued", a.invite_summary)

    def test_invite_cron_marks_sent(self):
        a = self._launchable(2)
        a.action_start()
        with patch("odoo.addons.auth_signup.models.res_users."
                   "ResUsers.action_reset_password"), \
                patch.object(self.env.cr, "commit"):
            self.Evaluator._cron_send_pending_invitations()
        for e in a.assessment_evaluator_ids:
            e.invalidate_recordset()
            self.assertEqual(e.invite_state, "sent")

    def test_invite_cron_flags_failed_then_requeue(self):
        a = self._launchable(1)
        a.action_start()
        ev = a.assessment_evaluator_ids

        def boom(*args, **kw):
            raise ValueError("SMTP unreachable")

        with patch("odoo.addons.auth_signup.models.res_users."
                   "ResUsers.action_reset_password", side_effect=boom), \
                patch.object(self.env.cr, "commit"):
            self.Evaluator._cron_send_pending_invitations()
        ev.invalidate_recordset()
        self.assertEqual(ev.invite_state, "failed")
        self.assertTrue(ev.invite_error)
        ev.action_requeue_invitation()    # retry
        ev.invalidate_recordset()
        self.assertEqual(ev.invite_state, "queued")
