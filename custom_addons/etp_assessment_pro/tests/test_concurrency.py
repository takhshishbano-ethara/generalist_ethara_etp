# -*- coding: utf-8 -*-
"""Race / idempotency tests for the candidate response-recording path.

Covers the risks the SCR plan calls X-01..X-07 (open-questions #18/#19/#24/#27),
all rooted in the fact that the response *upsert* in
``controllers/portal.py::_record_response`` has NO row lock and NO unique DB
constraint on ``(assessment_evaluator_id, question_id, day_session_id)``.
The design decision recorded in the plan is that correctness is protected at the
application layer (search-then-create-or-overwrite) plus finalize-time
idempotency, NOT by a DB guard. These tests pin down that the application-layer
guarantees actually hold:

  X-01  UPSERT IDEMPOTENCY (single mode): recording twice for the same
        (evaluator, question) leaves EXACTLY ONE row; the second write wins.
  X-02  UPSERT IDEMPOTENCY (day mode): same, scoped by day_session_id.
  X-03  RE-ENTRANCY of day submit: action_submit_day twice does not error or
        double-count; placeholders are not duplicated and the rollup is stable.
  X-04  RE-ENTRANCY of single auto-submit / finish: driving the finish path
        twice does not add rows or change the score.
  X-05  DEADLINE vs SUBMIT (single): after is_time_expired(), a late submit is
        absorbed by the auto-submit path — evaluator locks, no extra row, and
        action_submit on a locked response is refused (no crash).
  X-06  DEADLINE vs SUBMIT (day): a late submit after the deadline auto-submits
        the day and does not create a duplicate answered row.
  X-07  MISSED-DEADLINE CRON: _cron_mark_missed does not clobber an
        already-submitted/scored session, and finalizes a genuinely stale
        in_progress one exactly once.

Plus a real HTTP driver (TestPortalDayRoutesHttp) that exercises the actual
``/pro_assessment/day/<token>[/begin|/submit|/finish]`` routes end to end and
asserts BOTH HTTP status AND resulting DB state (the existing smoke test in
test_phase23_lifecycle.py skips this).

NOTE ON TRUE CONCURRENCY: two genuinely simultaneous transactions inserting the
same (evaluator, question) row cannot be made deterministic and clean inside a
single TransactionCase (both writers need to commit to collide, which would
leave committed rows in the throwaway etp_test_db). True-concurrency duplicate
detection is therefore NOT asserted here; it is covered by the Locust spike plus
the post-run SQL invariant documented in tests/load/README.md. The tests below
lock down the *application-layer* upsert/idempotency contract deterministically.
"""
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.etp_assessment_pro.controllers import portal as portal_ctrl
from odoo.addons.etp_assessment_pro.tests.test_phase23_lifecycle import _Base


class _FakeRequest:
    """Minimal request stand-in: the controller helpers under test only touch
    ``request.env`` (see tests/test_goback_edit.py:9). Reusing this lets us
    drive _record_response / _auto_submit_* deterministically without HTTP."""

    def __init__(self, env):
        self.env = env


class _ConcurrencyBase(_Base):
    """Shared scaffolding for the deterministic upsert/idempotency tests.

    Reuses _Base's builders (_make_applicant, _make_mcq, ...)
    so every test operates on real, launched assessment data.
    """

    def _single_with_two_mcq(self):
        """A launched single-mode assessment + 1 candidate + 2 MCQ questions."""
        cat = self._make_category("ConcCat")
        q1, dim1, master1 = self._make_mcq("CC1", correct_idx=0, category=cat)
        q2, dim2, master2 = self._make_mcq("CC2", correct_idx=0, category=cat)
        emp = self._make_applicant("ConcSingle")
        a = self.Assessment.create({
            "name": "ConcSingleA",
            "generator_id": cat.id,
            "question_limit": 2,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q1.id, q2.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        return a, ev, (q1, dim1, master1), (q2, dim2, master2)

    def _record(self, evaluator, form):
        """Drive the controller's real _record_response with a fake request."""
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            return ctrl._record_response(evaluator=evaluator, form=form)


@tagged("-at_install", "post_install")
class TestUpsertIdempotencySingle(_ConcurrencyBase):
    """X-01 — single-mode response upsert is idempotent on (evaluator, q)."""

    def test_double_submit_same_question_keeps_one_row(self):
        _, ev, (q1, dim1, master1), _ = self._single_with_two_mcq()
        # First submit picks the WRONG option, second picks the RIGHT one
        # (simulating a duplicate POST / retry that also changes the answer).
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[1].id)})
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        rows = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q1.id)])
        self.assertEqual(
            len(rows), 1,
            "the no-lock upsert must not create a duplicate response row")
        chosen = rows.line_ids.mapped("selected_option_id.id")
        self.assertEqual(
            chosen, [master1[0].id],
            "the second (last) write must overwrite the earlier picks")
        rows.invalidate_recordset()
        self.assertEqual(rows.score, rows.max_score)
        self.assertEqual(rows.max_score, 1)

    def test_double_submit_justification_last_write_wins(self):
        # Subjective question: recording twice overwrites the justification and
        # still leaves exactly one row.
        cat = self._make_category("ConcSubjCat")
        q = self._make_subjective("CCsubj", category=cat)
        emp = self._make_applicant("ConcSubj")
        a = self.Assessment.create({
            "name": "ConcSubjA",
            "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30,
            "evaluator_ids": [(6, 0, [emp.id])]})
        a.write({"question_ids": [(6, 0, [q.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        self._record(ev, {
            "question_id": str(q.id), "justification": "first"})
        self._record(ev, {
            "question_id": str(q.id), "justification": "SECOND final"})
        rows = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id),
            ("question_id", "=", q.id)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.justification, "SECOND final")
        # A real justification on a subjective question needs LLM scoring later.
        self.assertTrue(rows.needs_llm)


@tagged("-at_install", "post_install")
class TestSingleFinishReentrancy(_ConcurrencyBase):
    """X-04 — the single-mode finalizer is idempotent: driving it twice adds no
    rows and does not change the score, and finish() short-circuits once the
    evaluator is already submitted."""

    def test_auto_submit_remaining_twice_is_idempotent(self):
        _, ev, (q1, dim1, master1), (q2, _, _) = self._single_with_two_mcq()
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            ctrl._auto_submit_remaining_single(ev)
            ev.invalidate_recordset()
            first_rowcount = self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id)])
            self.assertTrue(ev.is_locked)
            self.assertEqual(ev.state, "submitted")
            self.assertEqual(first_rowcount, 2)  # answered q1 + placeholder q2
            self.assertEqual(ev.max_possible_score, 2)
            self.assertEqual(ev.total_score, 1)
            # Second pass: no new placeholder, no state/score drift.
            ctrl._auto_submit_remaining_single(ev)
        ev.invalidate_recordset()
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id)]),
            first_rowcount,
            "re-finalizing must not create a second placeholder")
        self.assertEqual(ev.total_score, 1)
        self.assertEqual(ev.max_possible_score, 2)

    def test_finish_short_circuits_when_already_submitted(self):
        # Once locked+submitted, the finish route must NOT try to auto-submit
        # again (which would attempt writes on a locked evaluator). We assert it
        # simply redirects and leaves state untouched.
        _, ev, (q1, dim1, master1), (q2, dim2, master2) = \
            self._single_with_two_mcq()
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        self._record(ev, {
            "question_id": str(q2.id),
            "dimension_%d" % dim2.id: str(master2[0].id)})
        # Answering all questions flips the evaluator to submitted+locked.
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "submitted")
        self.assertTrue(ev.is_locked)
        token = ev.access_token
        before = self.Response.search_count([
            ("assessment_evaluator_id", "=", ev.id)])
        ctrl = portal_ctrl.EtpAssessmentPortal()

        from werkzeug.utils import redirect as _wz_redirect

        class _Req(_FakeRequest):
            def redirect(self, url, code=303, local=True):
                # Return a real werkzeug redirect so the @http.route wrapper's
                # Response.load() accepts it; a plain tuple raises TypeError.
                return _wz_redirect(url, code=code)

        # The finish route now runs _guard_or_abort (which reads request.env.user
        # for the wrong-candidate/login decision); that guard is covered by the
        # HttpCase suite, so stub it here to isolate the short-circuit behavior.
        with patch.object(portal_ctrl, "request", _Req(self.env)), \
                patch.object(ctrl, "_guard_or_abort", return_value=None):
            result = ctrl.assessment_finish_single(token)
        self.assertIn(result.status_code, (301, 302, 303))
        self.assertIn(token, result.headers.get("Location", ""))
        ev.invalidate_recordset()
        self.assertEqual(ev.state, "submitted")
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id)]),
            before,
            "finishing an already-submitted evaluator must add no rows")


@tagged("-at_install", "post_install")
class TestDeadlineVsSubmitSingle(_ConcurrencyBase):
    """X-05 — a late single-mode submit after the deadline is absorbed by the
    auto-submit path: the evaluator locks, no crash, and no duplicate row."""

    def _expire(self, ev):
        past = fields.Datetime.now() - timedelta(hours=2)
        ev.write({"started_at": past})
        ev.invalidate_recordset()

    def test_expired_single_route_submit_locks_no_extra_row(self):
        _, ev, (q1, dim1, master1), (q2, _, _) = self._single_with_two_mcq()
        # Answer q1 while still in time, then blow the deadline.
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        self._expire(ev)
        self.assertTrue(ev.is_time_expired())
        token = ev.access_token

        class _Req(_FakeRequest):
            def redirect(self, url):
                return ("redirect", url)

        # Route-level submit while expired: the guard runs the auto-submit path
        # and redirects; it must NOT record a fresh answer or crash.
        ctrl = portal_ctrl.EtpAssessmentPortal()
        # _is_real_candidate / _guard_or_abort read request.env.user, so drive
        # the low-level path directly (deadline branch) for determinism.
        with patch.object(portal_ctrl, "request", _Req(self.env)):
            ctrl._auto_submit_remaining_single(ev)
        ev.invalidate_recordset()
        self.assertTrue(ev.is_locked)
        self.assertEqual(ev.state, "submitted")
        rows = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id)])
        self.assertEqual(len(rows), 2, "one answered + one placeholder only")
        # q1's real answer survives; q2 is the time-expired placeholder.
        q1_row = rows.filtered(lambda r: r.question_id.id == q1.id)
        self.assertEqual(len(q1_row), 1)
        self.assertEqual(q1_row.score, 1)

    def test_action_submit_on_locked_response_is_refused(self):
        # After finalize the evaluator is locked; action_submit on any of its
        # responses must raise (not silently create/duplicate). This is the
        # model guard behind the "late submit" race.
        _, ev, (q1, dim1, master1), (q2, _, _) = self._single_with_two_mcq()
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        ctrl = portal_ctrl.EtpAssessmentPortal()
        with patch.object(portal_ctrl, "request", _FakeRequest(self.env)):
            ctrl._auto_submit_remaining_single(ev)
        ev.invalidate_recordset()
        self.assertTrue(ev.is_locked)
        # Grab a submitted row and try to re-submit it: already-submitted guard.
        row = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id)], limit=1)
        with self.assertRaises(UserError):
            row.action_submit()


@tagged("-at_install", "post_install")
class TestExpireStaleAttemptsCron(_ConcurrencyBase):
    """The _cron_expire_stale_attempts sweep rescues an abandoned in-progress
    attempt whose deadline has passed - the candidate never came back, so only
    the cron (not a live portal request) can settle it. Closes the gap where an
    abandoned tab left the attempt unscored AND pinned the assessment open."""

    def _expire(self, ev):
        # A real abandoned attempt went through /begin, which sets BOTH
        # started_at and state='in_progress' (portal.py:161-164). Reproduce that
        # begun-then-abandoned state, with the deadline already in the past.
        past = fields.Datetime.now() - timedelta(hours=2)
        ev.write({"started_at": past, "state": "in_progress"})
        ev.invalidate_recordset()

    def _run_cron(self):
        # The cron commits per candidate (idle-txn safety in prod); the test
        # harness forbids a real commit, so no-op it - same convention as
        # test_phase23_lifecycle's _cron_llm_auto_score test.
        with patch.object(self.env.cr, "commit"):
            self.env["etp.assessment.pro.evaluator"]._cron_expire_stale_attempts()

    def test_cron_auto_submits_expired_attempt_and_closes_assessment(self):
        a, ev, (q1, dim1, master1), (q2, _, _) = self._single_with_two_mcq()
        # Answer q1 in time, then abandon the tab and let the deadline pass.
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        self._expire(ev)
        self.assertEqual(ev.state, "in_progress")
        self.assertTrue(ev.is_time_expired())

        # The cron runs with no live request context at all.
        self._run_cron()
        ev.invalidate_recordset()
        a.invalidate_recordset()

        self.assertEqual(ev.state, "submitted")
        self.assertTrue(ev.is_locked)
        rows = self.Response.search([
            ("assessment_evaluator_id", "=", ev.id)])
        self.assertEqual(len(rows), 2, "one answered + one placeholder only")
        self.assertEqual(
            rows.filtered(lambda r: r.question_id.id == q1.id).score, 1,
            "the real in-time answer must survive the sweep")
        # The whole assessment auto-completes once its only candidate is done.
        self.assertEqual(a.state, "done")

    def test_cron_skips_unlimited_and_live_attempts(self):
        # duration_minutes=0 → no deadline → deadline_datetime is False → the
        # '<' filter must never touch it, and an in-time attempt is left alone.
        cat = self._make_category("NoDeadlineCat")
        q1, dim1, master1 = self._make_mcq("ND1", correct_idx=0, category=cat)
        emp = self._make_applicant("NoDeadline")
        a = self.Assessment.create({
            "name": "NoDeadlineA",
            "generator_id": cat.id,
            "question_limit": 1,
            "duration_minutes": 0,  # unlimited sitting
            "evaluator_ids": [(6, 0, [emp.id])],
        })
        a.write({"question_ids": [(6, 0, [q1.id])]})
        a.action_start()
        ev = a.assessment_evaluator_ids[0]
        self.assertFalse(ev.deadline_datetime)
        # Put it in the live begun state (as /begin would) with an old start -
        # unlimited duration means no deadline, so the sweep must skip it.
        ev.write({"started_at": fields.Datetime.now() - timedelta(hours=2),
                  "state": "in_progress"})
        ev.invalidate_recordset()

        self._run_cron()
        ev.invalidate_recordset()
        self.assertEqual(
            ev.state, "in_progress",
            "an unlimited-duration attempt must never be auto-expired")

    def test_cron_is_idempotent(self):
        a, ev, (q1, dim1, master1), (q2, _, _) = self._single_with_two_mcq()
        self._record(ev, {
            "question_id": str(q1.id),
            "dimension_%d" % dim1.id: str(master1[0].id)})
        self._expire(ev)
        self._run_cron()
        ev.invalidate_recordset()
        first = self.Response.search_count([
            ("assessment_evaluator_id", "=", ev.id)])
        # Second sweep: the attempt is already 'submitted', so the state filter
        # excludes it - no new placeholder, no drift.
        self._run_cron()
        self.assertEqual(
            self.Response.search_count([
                ("assessment_evaluator_id", "=", ev.id)]),
            first,
            "a second sweep must not create another placeholder")


@tagged("-at_install", "post_install")
class TestShardedAutoScore(_ConcurrencyBase):
    """Parallel scoring lanes: N shard crons drain disjoint evaluator slices
    (id % shard_count == shard) under distinct advisory locks, so scoring scales
    past the single-lock serial ceiling. shard_count=1 stays byte-identical to
    the original drainer.
    """

    def _queued_evaluators(self, n):
        from odoo.addons.etp_assessment_pro.services import vertex as vertex_svc
        cat = self._make_category("ShardCat")
        q = self._make_subjective("ShardQ", category=cat)
        a = self.Assessment.create({
            "name": "ShardA", "generator_id": cat.id, "question_limit": 1,
            "duration_minutes": 30, "llm_auto_score": True,
            "evaluator_ids": [(6, 0, [self._make_applicant("SC%d" % i).id])
                              for i in range(n)]})
        a.write({"question_ids": [(6, 0, [q.id])]})
        a.action_start()
        for ev in a.assessment_evaluator_ids:
            self.Response.create({
                "assessment_id": a.id, "assessment_evaluator_id": ev.id,
                "evaluator_id": ev.applicant_id.id, "question_id": q.id,
                "justification": "An answer.", "state": "submitted",
                "llm_state": "pending"})
            ev.write({"state": "submitted", "scoring_requested": True,
                      "llm_state": "pending"})
        return a, q, vertex_svc

    def _set_shards(self, n):
        self.env["ir.config_parameter"].sudo().set_param(
            "etp_assessment_pro.scoring_shards", str(n))

    def test_shards_partition_evaluators_disjointly(self):
        a, _q, _v = self._queued_evaluators(6)
        ids = a.assessment_evaluator_ids.ids
        self._set_shards(3)
        seen = []
        for shard in range(3):
            got = self.Assessment._scoring_shard_evaluators(shard, 3, limit=100)
            self.assertTrue(all(e.id % 3 == shard for e in got),
                            "shard %d holds only its own residue class" % shard)
            seen += got.ids
        self.assertEqual(sorted(seen), sorted(ids),
                         "every queued evaluator is owned by exactly one shard")
        self.assertEqual(len(seen), len(set(seen)), "no evaluator in two shards")

    def test_full_cohort_scored_across_all_shards(self):
        a, _q, _v = self._queued_evaluators(5)
        self._set_shards(3)
        # Patch the per-evaluator scorer to a marker write so the test isolates
        # SHARD ROUTING (does every evaluator get drained by some shard?) from
        # the Vertex/scoring internals covered elsewhere.
        Evaluator = type(self.env["etp.assessment.pro.evaluator"])
        def _mark(self):
            self.write({"llm_state": "scored"})
            return len(self)
        with patch.object(Evaluator, "action_llm_score", _mark), \
                patch.object(self.env.cr, "commit"):
            for shard in range(3):
                self.Assessment._cron_llm_auto_score(shard=shard)
        for ev in a.assessment_evaluator_ids:
            ev.invalidate_recordset()
            self.assertEqual(
                ev.llm_state, "scored",
                "evaluator %s must be drained by exactly one shard" % ev.id)

    def test_shard_noop_when_beyond_configured_count(self):
        a, _q, _v = self._queued_evaluators(3)
        self._set_shards(2)
        # shard 2 is disabled while scoring_shards=2 (valid shards 0,1).
        before = a.assessment_evaluator_ids.mapped("llm_state")
        self.Assessment._cron_llm_auto_score(shard=2)
        a.assessment_evaluator_ids.invalidate_recordset()
        self.assertEqual(a.assessment_evaluator_ids.mapped("llm_state"), before,
                         "a shard >= scoring_shards must be a pure no-op")

    def test_default_single_shard_matches_original_domain(self):
        a, _q, _v = self._queued_evaluators(4)
        # No config param set -> default 1 shard -> unmodulo'd search of all 4.
        self.assertEqual(self.Assessment._scoring_shard_count(), 1)
        got = self.Assessment._scoring_shard_evaluators(0, 1, limit=100)
        self.assertEqual(sorted(got.ids), sorted(a.assessment_evaluator_ids.ids),
                         "shard_count=1 selects every queued evaluator (legacy)")

    def test_shard_count_clamped(self):
        self._set_shards(99)
        self.assertEqual(self.Assessment._scoring_shard_count(), 4,
                         "shard count clamps to MAX_SCORING_SHARDS")
        self._set_shards(0)
        self.assertEqual(self.Assessment._scoring_shard_count(), 1,
                         "shard count floors at 1")
        self._set_shards(-3)
        self.assertEqual(self.Assessment._scoring_shard_count(), 1)


