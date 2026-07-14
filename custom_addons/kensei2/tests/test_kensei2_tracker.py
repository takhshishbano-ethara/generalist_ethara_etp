# -*- coding: utf-8 -*-
"""Kensei2 Tracker — allocation pipeline, access control and stage hand-off.

The Tracker had no tests at all. These pin the two things that were actually
broken and the logic that makes them dangerous:

  * ACCESS CONTROL — the ``_guard_privileged_fields`` / ``_check_locked`` guards
    are the ONLY thing between a tasker and another tasker's work (the view's
    ``readonly`` is cosmetic and trivially bypassed over RPC). Each historical
    bypass gets a named regression test.

  * THE STATUS LADDER — ``_compute_status`` is a nested ladder with two divergent
    stage pipelines, and ``date_final`` (delivery credit on the Daily Tracker) is
    derived from it. A silent break here mis-credits people's work.

Read ``test_etp_only_user_cannot_see_other_taskers`` first: it is the case the
original design forgot, and the reason a whole ETP role ladder had unscoped
read/write on every allocation in the table.
"""
import json
import re
import uuid
from unittest.mock import patch

from odoo.addons.kensei2.controllers import tracker
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase


def employee_for(env, user):
    """The user's hr.employee, created if this database does not auto-create one.

    The ETP databases spawn an employee per res.users; a bare Odoo install does not.
    These tests NEED the PL to resolve to an hr.employee, because that IS the point:
    pl_id is an hr.employee, a PL is not an HR user, and reading .name off it raises
    AccessError. With no employee, pl_id is empty and the tests pass VACUOUSLY.

    Module-level, not a classmethod: both the TransactionCase base and the HttpCase
    classes need it, and they do not share a parent.
    """
    Emp = env["hr.employee"].sudo()
    emp = Emp.search([("user_id", "=", user.id)], limit=1)
    if emp:
        return emp
    return Emp.with_context(etp_importing=True).create({
        "name": user.name,
        "user_id": user.id,
        "work_email": user.email or ("%s@ethara.ai" % user.login.split("@")[0]),
    })


@tagged("post_install", "-at_install")
class Kensei2TrackerCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alloc = cls.env["kensei2.tracker.allocation"]
        cls.Member = cls.env["kensei2.tracker.team.member"]
        cls.Persona = cls.env["kensei2.persona"]

        cls.persona = cls.Persona.create({"name": "tracker-test-persona"})
        cls.persona2 = cls.Persona.create({"name": "tracker-test-persona-2"})

        cls.g_tasker = cls.env.ref("kensei2.group_kensei2_tasker")
        cls.g_ql = cls.env.ref("kensei2.group_kensei2_ql")
        cls.g_pl = cls.env.ref("kensei2.group_kensei2_pl")
        cls.g_admin = cls.env.ref("kensei2.group_kensei2_admin")
        cls.g_etp_tasker = cls.env.ref("etp_user_roles.group_tasker")

        cls.user_tasker = cls._make_user("kt.tasker", cls.g_tasker)
        cls.user_other = cls._make_user("kt.other", cls.g_tasker)
        cls.user_ql = cls._make_user("kt.ql", cls.g_ql)
        # Two rival leads, so "team" scoping is actually provable: each must see
        # their own team's allocations and NOT the other's.
        cls.user_pl = cls._make_user("kt.pl", cls.g_pl)
        cls.user_pl2 = cls._make_user("kt.pl2", cls.g_pl)
        cls.user_admin = cls._make_user("kt.admin", cls.g_admin)
        # An ETP-ladder user holding NO kensei2.* group. This is the population the
        # original record rules never bound to.
        cls.user_etp = cls._make_user("kt.etp", cls.g_etp_tasker)

        # Roster members carry a lead, the way real ones do — the allocation then
        # inherits assigned_pl_id / assigned_ql_id from them (_compute_assignments).
        # Without a lead an allocation is an UNASSIGNED row, which under the
        # three-tier model only an Admin may see; a QL could not even write to it.
        cls.member_tasker = cls._make_member(
            cls.user_tasker, pl=cls.user_pl, ql=cls.user_ql)
        cls.member_other = cls._make_member(
            cls.user_other, pl=cls.user_pl, ql=cls.user_ql)

    # ------------------------------------------------------------------ #
    @classmethod
    def _make_user(cls, login, group):
        """A res.users in ``group``.

        Two deployment-specific fixture details, both from other ETP modules:

        * ``@ethara.ai`` — hr.employee constrains work emails to the corporate
          domain, and this DB auto-creates an employee for every user.
        * ``etp_importing`` — employee_role_import requires an ETP-Tasker employee
          to have a QL assigned in the Task Force hierarchy. That is a real
          production rule but irrelevant here (we are testing Kensei2's record
          rules, not ETP's org chart), so we use the module's own documented
          bypass rather than building a whole org tree per test.
        """
        return cls.env["res.users"].with_context(etp_importing=True).create({
            "name": login,
            "login": "%s@ethara.ai" % login,
            "email": "%s@ethara.ai" % login,
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, group.id])],
        })

    @classmethod
    def _make_member(cls, user, pl=None, ql=None):
        vals = {"user_id": user.id, "status": "active"}
        if pl:
            vals["assigned_pl_id"] = pl.id
        if ql:
            vals["assigned_ql_id"] = ql.id
        return cls.Member.with_context(kensei2_skip_toast=True).create(vals)


    # suffix -> uuid. Task IDs are UUIDs now, but the tests still want to talk about
    # "the reset task" rather than a hex string, so each suffix is given a stable
    # uuid for the life of the run. Tests that need the raw value read alloc.task_id.
    _uuid_by_suffix = {}

    @classmethod
    def _uuid_for(cls, suffix):
        if suffix not in cls._uuid_by_suffix:
            cls._uuid_by_suffix[suffix] = str(uuid.uuid4())
        return cls._uuid_by_suffix[suffix]

    @classmethod
    def _make_alloc(cls, member=None, **overrides):
        vals = {
            "task_id": cls._uuid_for(overrides.pop("suffix", "001")),
            "persona_id": cls.persona.id,
            "tasker_member_id": (member or cls.member_tasker).id,
        }
        vals.update(overrides)
        return cls.Alloc.create(vals)

    @staticmethod
    def _complete_stage1(alloc):
        """Drive a stage-1 allocation all the way down its pipeline."""
        alloc.write({
            "drive_link": "https://drive.example.com/a",
            "pl_verified_status": "done",
            "baseline_ready_status": "done",
            "baseline_drive_link": "https://drive.example.com/b",
            "baseline_gen_status": "done",
            "qced_by": alloc.env.user.id,
            "manual_qc_status": "done",
        })


class TestTrackerAccessControl(Kensei2TrackerCommon):
    """Regression tests for each historical bypass of the privileged-field guard."""

    def test_etp_only_user_cannot_see_other_taskers(self):
        """SEC-1: an ETP-ladder user with no kensei2.* group must NOT see everything.

        ir.model.access.csv grants etp_user_roles.group_tasker access to this model,
        but no record rule used to bind to that group. Odoo skips rules whose groups
        don't intersect the user's, then ANDs the globals with the OR of what
        survives — so an empty rule set collapses to TRUE. Result: every ETP role,
        up the whole chain to CFO, had UNRESTRICTED read/write on every allocation.
        """
        mine = self._make_alloc(suffix="etp-mine")
        # sanity: the ETP user really does hold no kensei2 group
        self.assertFalse(
            self.user_etp.has_group("kensei2.group_kensei2_tasker"),
            "fixture is wrong — the ETP user must hold no kensei2.* group",
        )
        visible = self.Alloc.with_user(self.user_etp).search([])
        self.assertNotIn(
            mine, visible,
            "an ETP-only user can see another tasker's allocation — the record "
            "rules do not bind to the ETP group ladder",
        )

    def test_tasker_cannot_reassign_via_tasker_email(self):
        """SEC-2: tasker_email is the same control surface as tasker_member_id.

        _sync_tasker resolves an email back to a member and writes
        tasker_member_id from it. While the guard ran BEFORE the sync and did not
        list tasker_email, a tasker could hand their own task to someone else by
        writing an email — the guard never saw a protected key.
        """
        alloc = self._make_alloc(suffix="reassign")
        with self.assertRaises(AccessError):
            alloc.with_user(self.user_tasker).write(
                {"tasker_email": self.user_other.email})
        alloc.invalidate_recordset()
        self.assertEqual(alloc.tasker_member_id, self.member_tasker)

    def test_tasker_cannot_zero_total_stages(self):
        """SEC-3: the guard tested truthiness, so every FALSY value slipped through.

        total_stages=0 makes is_final_stage (stage_no >= total_stages) True on
        stage 1, which lets a tasker's completed stage 1 reach 'deliverable'
        instead of 'ready_next_stage' — self-certifying delivery and skipping
        stage 2 entirely.
        """
        alloc = self._make_alloc(suffix="zero")
        with self.assertRaises(AccessError):
            alloc.with_user(self.user_tasker).write({"total_stages": 0})
        alloc.invalidate_recordset()
        self.assertEqual(alloc.total_stages, 2)
        self.assertFalse(alloc.is_final_stage)

    def test_tasker_cannot_clear_assigned_date(self):
        """SEC-3, same root cause: False is falsy, so clearing was never blocked."""
        alloc = self._make_alloc(suffix="cleardate")
        with self.assertRaises(AccessError):
            alloc.with_user(self.user_tasker).write({"assigned_date": False})

    def test_tasker_cannot_unlock_via_context(self):
        """SEC-4: `kensei2_reopen` was honoured on the context key alone.

        Context is attacker-supplied — it travels with every RPC call — so a
        context flag can state intent but can never prove privilege. A tasker
        passing it could edit a frozen Deliverable record, overwriting the scores
        the next stage and the Daily Tracker were derived from.
        """
        alloc = self._make_alloc(suffix="locked", stage_no=2, parent_id=False)
        alloc.write({
            "baseline_drive_link": "https://drive.example.com/b",
            "baseline_gen_status": "done",
            "qced_by": self.env.user.id,
            "manual_qc_status": "done",
        })
        self.assertEqual(alloc.status, "deliverable")
        self.assertTrue(alloc.is_locked)

        with self.assertRaises(UserError):
            alloc.with_user(self.user_tasker).with_context(
                kensei2_reopen=True).write({"rubric_score": 99.0})

    def test_tasker_may_still_fill_own_stage_inputs(self):
        """The guard must not over-reach: a tasker drives their own stage end to end."""
        alloc = self._make_alloc(suffix="ownwork")
        alloc.with_user(self.user_tasker).write({
            "drive_link": "https://drive.example.com/a",
            "tasker_qc_notes": "done authoring",
        })
        self.assertEqual(alloc.status, "tasker_qc_completed")

    def test_ql_may_reassign(self):
        """The guard must not over-reach the other way either."""
        alloc = self._make_alloc(suffix="qlreassign")
        alloc.with_user(self.user_ql).write(
            {"tasker_member_id": self.member_other.id})
        self.assertEqual(alloc.tasker_member_id, self.member_other)


class TestTrackerVisibilityTiers(Kensei2TrackerCommon):
    """The three-tier access model, enforced in the RECORD RULES.

        Admin    every allocation, including rows with no PL/QL assigned
        PL / QL  their own team only (assigned_pl_id / assigned_ql_id)
        Tasker   only the allocations assigned to them

    Before 19.0.1.10.0 only the Tasker tier was real: QL and PL both carried
    [(1,'=',1)], so a lead saw a team-scoped Dashboard (controllers/tracker.py
    scopes it) and then saw EVERY team the moment they opened the list view.
    These tests exist so the rules and the controller can never drift apart again.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Both lead fields are set EXPLICITLY on every row (the roster member
        # defaults them, so relying on the default would not prove scoping).
        # PL1's team:
        cls.a_pl1 = cls._make_alloc(suffix="team-pl1")
        cls.a_pl1.write({"assigned_pl_id": cls.user_pl.id,
                         "assigned_ql_id": False})
        # PL2's team — the row PL1 and the QL must NOT be able to see:
        cls.a_pl2 = cls._make_alloc(suffix="team-pl2", member=cls.member_other)
        cls.a_pl2.write({"assigned_pl_id": cls.user_pl2.id,
                         "assigned_ql_id": False})
        # Led by the QL, with no PL:
        cls.a_ql = cls._make_alloc(suffix="team-ql", persona_id=cls.persona2.id)
        cls.a_ql.write({"assigned_ql_id": cls.user_ql.id,
                        "assigned_pl_id": False})
        # No lead at all — only an Admin may see this.
        cls.a_orphan = cls._make_alloc(suffix="team-orphan")
        cls.a_orphan.write({"assigned_pl_id": False, "assigned_ql_id": False})

    def _visible(self, user):
        return self.Alloc.with_user(user).search([])

    def test_pl_sees_only_their_own_team(self):
        visible = self._visible(self.user_pl)
        self.assertIn(self.a_pl1, visible, "a PL must see their own team's task")
        self.assertNotIn(
            self.a_pl2, visible,
            "a PL can see ANOTHER PL's team — the rule is not team-scoped")

    def test_ql_sees_only_tasks_they_lead(self):
        visible = self._visible(self.user_ql)
        self.assertIn(self.a_ql, visible)
        self.assertNotIn(
            self.a_pl2, visible,
            "a QL can see a task they do not lead — the rule is not team-scoped")

    def test_unassigned_rows_are_admin_only(self):
        """A row with no PL/QL is invisible to every lead, by design."""
        self.assertNotIn(self.a_orphan, self._visible(self.user_pl))
        self.assertNotIn(self.a_orphan, self._visible(self.user_ql))
        self.assertIn(self.a_orphan, self._visible(self.user_admin))

    def test_admin_sees_every_team(self):
        """Guards a trap: group_kensei2_admin IMPLIES group_kensei2_pl, so without a
        rule of its own an Admin would silently inherit the PL team-scope and stop
        being org-wide the moment the PL rule was tightened."""
        visible = self._visible(self.user_admin)
        for alloc in (self.a_pl1, self.a_pl2, self.a_ql, self.a_orphan):
            self.assertIn(alloc, visible,
                          "Admin must see the whole organisation")

    def test_lead_still_sees_a_task_assigned_to_them_as_tasker(self):
        """Rules OR together: a lead who is also the tasker on a task keeps it."""
        member_pl = self._make_member(self.user_pl2)
        own = self._make_alloc(suffix="lead-own", member=member_pl)
        own.write({"assigned_pl_id": self.user_pl.id})  # PL1's team, PL2 is tasker
        self.assertIn(own, self._visible(self.user_pl2),
                      "a lead lost sight of a task assigned to them as tasker")

    def test_tasker_tier_unaffected(self):
        self.assertNotIn(self.a_pl2, self._visible(self.user_tasker))


class TestTrackerStatusLadder(Kensei2TrackerCommon):
    """_compute_status is a NESTED ladder: every rung re-checks the ones below it."""

    def test_stage1_ladder_climbs_in_order(self):
        alloc = self._make_alloc(suffix="ladder")
        self.assertEqual(alloc.status, "in_progress")

        alloc.drive_link = "https://drive.example.com/a"
        self.assertEqual(alloc.status, "tasker_qc_completed")

        # BOTH sign-offs are required — either alone must not advance the task
        alloc.pl_verified_status = "done"
        self.assertEqual(alloc.status, "tasker_qc_completed")
        alloc.baseline_ready_status = "done"
        self.assertEqual(alloc.status, "ready_baseline")

        alloc.baseline_drive_link = "https://drive.example.com/b"
        self.assertEqual(alloc.status, "baseline_generated")

        alloc.write({"baseline_gen_status": "done", "qced_by": self.env.user.id})
        self.assertEqual(alloc.status, "manual_qc")

        alloc.manual_qc_status = "done"
        # stage 1 of 2 is NOT the final stage: finishing it does not deliver
        self.assertEqual(alloc.status, "ready_next_stage")
        self.assertEqual(alloc.final_status, "in_progress")

    def test_clearing_an_early_input_demotes_the_record(self):
        """The whole point of the nested ladder — a flat series of ifs let the last
        rung win regardless of the ones below it, so a delivered task whose drive
        link was emptied kept its credit."""
        alloc = self._make_alloc(suffix="demote")
        self._complete_stage1(alloc)
        self.assertEqual(alloc.status, "ready_next_stage")
        self.assertTrue(alloc.date_final)

        alloc.drive_link = False
        self.assertEqual(alloc.status, "in_progress")
        # and the delivery credit the Daily Tracker had counted is withdrawn
        self.assertFalse(alloc.date_final)

    def test_failure_at_any_gate_terminates(self):
        alloc = self._make_alloc(suffix="fail")
        self._complete_stage1(alloc)
        self.assertEqual(alloc.status, "ready_next_stage")

        alloc.write({
            "manual_qc_status": "failed",
            "manual_qc_reason": "trajectory diverges from the rubric",
        })
        self.assertEqual(alloc.status, "failed")
        self.assertEqual(alloc.final_status, "failed")

    def test_failure_requires_a_reason(self):
        alloc = self._make_alloc(suffix="noreason")
        with self.assertRaises(ValidationError):
            alloc.pl_verified_status = "failed"

    def test_baseline_done_requires_qced_by(self):
        alloc = self._make_alloc(suffix="noqc")
        with self.assertRaises(ValidationError):
            alloc.write({
                "drive_link": "https://drive.example.com/a",
                "baseline_gen_status": "done",
            })

    def test_stage2_starts_at_ready_baseline(self):
        """Stage 2 inherits an authored, verified task, so it runs the SHORT
        pipeline — the three authoring rungs are already behind it."""
        alloc = self._make_alloc(suffix="s2", stage_no=2)
        self.assertEqual(alloc.status, "ready_baseline")
        self.assertTrue(alloc.is_final_stage)

        alloc.baseline_drive_link = "https://drive.example.com/b"
        self.assertEqual(alloc.status, "baseline_generated")
        alloc.write({"baseline_gen_status": "done", "qced_by": self.env.user.id})
        self.assertEqual(alloc.status, "manual_qc")
        alloc.manual_qc_status = "done"
        # the FINAL stage is the only one that can deliver
        self.assertEqual(alloc.status, "deliverable")
        self.assertEqual(alloc.final_status, "deliverable")

    def test_drive_link_must_be_a_url(self):
        alloc = self._make_alloc(suffix="badurl")
        with self.assertRaises(ValidationError):
            alloc.drive_link = "not-a-url"


class TestTrackerTaskUuid(Kensei2TrackerCommon):
    """Task IDs are canonical UUIDs (8-4-4-4-12), and one UUID is one task."""

    _UUID_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

    def test_default_task_id_is_a_uuid(self):
        alloc = self.Alloc.create({
            "persona_id": self.persona.id,
            "tasker_member_id": self.member_tasker.id,
        })
        self.assertRegex(alloc.task_id, self._UUID_RE,
                         "a new allocation must default to a canonical UUID")

    def test_non_uuid_task_id_is_rejected(self):
        for bad in ("TA00127", "has spaces!", "not-a-uuid",
                    "123e4567-e89b-12d3-a456-42661417400",     # 11 in last group
                    "123e4567e89b12d3a456426614174000"):        # no dashes
            with self.assertRaises(ValidationError, msg="accepted %r" % bad):
                self._make_alloc(suffix="bad-%s" % bad, task_id=bad)

    def test_task_id_is_stored_lowercase(self):
        """The regex is case-insensitive, so without normalisation the SAME uuid in
        two cases would be two DIFFERENT strings — i.e. two different tasks that
        slip straight past unique(task_id, stage_no)."""
        upper = str(uuid.uuid4()).upper()
        alloc = self._make_alloc(suffix="case", task_id=upper)
        self.assertEqual(alloc.task_id, upper.lower())

    def test_two_different_tasks_never_share_a_uuid(self):
        made = [self.Alloc.create({
            "persona_id": self.persona.id,
            "tasker_member_id": self.member_tasker.id,
        }) for _ in range(5)]
        ids = [a.task_id for a in made]
        self.assertEqual(len(set(ids)), len(ids),
                         "two independently created tasks share a Task ID")

    def test_generator_skips_a_uuid_already_in_use(self):
        """_new_task_uuid() does an indexed SELECT before handing an ID out.

        A duplicate would NOT raise: the key is (task_id, stage_no), so a fresh
        stage-1 row colliding with an existing stage-2 row would insert cleanly and
        silently FUSE two unrelated tasks into one chain. Hence the check.
        """
        taken = self._make_alloc(suffix="taken").task_id
        seen = []

        real_uuid4 = uuid.uuid4

        def fake_uuid4():
            # hand out the taken id once, then a fresh one
            if not seen:
                seen.append(1)
                return uuid.UUID(taken)
            return real_uuid4()

        with patch.object(uuid, "uuid4", fake_uuid4):
            fresh = self.Alloc._new_task_uuid()

        self.assertNotEqual(fresh, taken,
                            "the generator handed out a Task ID already in use")
        self.assertRegex(fresh, self._UUID_RE)

    def test_stages_of_the_same_task_share_the_uuid(self):
        """The shared Task ID is what CHAINS the stages — it must not be unique
        per row, only per (task, stage)."""
        alloc = self._make_alloc(suffix="chain")
        self._complete_stage1(alloc)
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": alloc.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()

        stages = self.Alloc.search([("task_id", "=", alloc.task_id)])
        self.assertEqual(len(stages), 2)
        self.assertEqual(set(stages.mapped("stage_no")), {1, 2})
        self.assertEqual(len(set(stages.mapped("task_id"))), 1,
                         "the two stages of one task must share one Task ID")


class TestTrackerStageHandoff(Kensei2TrackerCommon):

    def _ready_stage1(self):
        alloc = self._make_alloc(suffix="handoff")
        self._complete_stage1(alloc)
        return alloc

    def test_handoff_creates_next_stage_clean(self):
        alloc = self._ready_stage1()
        self.assertTrue(alloc.can_hand_off)

        wizard = self.env["kensei2.tracker.stage.handoff"].with_user(
            self.user_ql).create({
                "allocation_id": alloc.id,
                "tasker_member_id": self.member_other.id,
            })
        wizard.action_confirm()

        nxt = self.Alloc.search([
            ("task_id", "=", alloc.task_id), ("stage_no", "=", 2)])
        self.assertEqual(len(nxt), 1)
        self.assertEqual(nxt.parent_id, alloc)
        self.assertEqual(nxt.tasker_member_id, self.member_other)
        self.assertEqual(nxt.persona_id, alloc.persona_id)
        # carried forward — it decides which stage may DELIVER
        self.assertEqual(nxt.total_stages, alloc.total_stages)
        # every stage input starts blank so the pipeline replays
        self.assertFalse(nxt.baseline_drive_link)
        self.assertFalse(nxt.manual_qc_status == "done")

        # the parent is no longer the current stage, so the task is counted ONCE
        alloc.invalidate_recordset()
        self.assertFalse(alloc.is_current_stage)
        self.assertTrue(nxt.is_current_stage)

    def test_handoff_locks_the_parent(self):
        alloc = self._ready_stage1()
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": alloc.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()

        alloc.invalidate_recordset()
        self.assertTrue(alloc.is_locked)
        # as a real user, NOT self.env: Environment forces su=True for SUPERUSER_ID
        # (orm/environments.py), and _check_locked deliberately exempts sudo — the
        # freeze guards users, not the ORM's own bookkeeping. Even a QL is blocked
        # here: to change a handed-off stage they must Reopen it first.
        with self.assertRaises(UserError):
            alloc.with_user(self.user_ql).write({"rubric_score": 50.0})


    def test_handoff_does_not_touch_stage_1s_data(self):
        """The reported "all my data got removed" bug — it is not one, and this
        pins it.

        Hand-off creates stage 2 EMPTY (it generates its own trajectory) and Odoo
        drops you straight onto that blank record. It looks like a wipe. It is not:
        stage 1 keeps every field. If this test ever fails, the wipe is real.
        """
        s1 = self._make_alloc(suffix="no-wipe")
        self._complete_stage1(s1)
        before = {f: s1[f] for f in (
            "drive_link", "baseline_drive_link", "baseline_gen_status",
            "manual_qc_status", "rubric_score", "pytest_score", "overall_score")}
        self.assertEqual(s1.status, "ready_next_stage")

        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": s1.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()
        s1.invalidate_recordset()

        # stage 1 is untouched
        for f, v in before.items():
            self.assertEqual(s1[f], v, "hand-off changed stage 1's %s" % f)
        self.assertEqual(s1.status, "ready_next_stage")

        # stage 2 is empty — that is the design, not a bug
        s2 = self.Alloc.search([("task_id", "=", s1.task_id), ("stage_no", "=", 2)])
        self.assertEqual(s2.status, "ready_baseline")
        self.assertFalse(s2.baseline_drive_link)
        self.assertEqual(s2.manual_qc_status, "in_progress")

    def test_display_name_names_the_stage(self):
        """Both stages share the task_id, so without the stage the "Handed off from"
        link shows the record its own UUID — as if handed off from itself."""
        s1 = self._make_alloc(suffix="dispname")
        self.assertEqual(s1.display_name, "%s (Stage 1)" % s1.task_id)
        self._complete_stage1(s1)
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": s1.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()
        s2 = self.Alloc.search([("task_id", "=", s1.task_id), ("stage_no", "=", 2)])
        self.assertEqual(s2.display_name, "%s (Stage 2)" % s2.task_id)
        self.assertNotEqual(s1.display_name, s2.display_name,
                            "the two stages display identically — the parent link is unreadable")


    def test_every_locked_input_is_force_save(self):
        """THE "my stage-2 data vanished on save" BUG.

        Every stage input is gated `readonly="is_locked"`, and is_locked is computed
        from `status`, which is computed from those very inputs. So filling the LAST
        field flips status -> 'deliverable', which flips is_locked -> True, which
        turns the fields the user just filled READONLY — mid-edit, via onchange.

        Odoo does not send readonly fields in web_save. The payload came out empty,
        web_save was never called (confirmed in the server log: 9 onchange calls, 0
        web_save), the client discarded the edits, and the form re-read the untouched
        record. The data looked deleted; it had simply never been sent.

        force_save="1" is the documented escape hatch: send the field even though it
        turned readonly. Without it on EVERY locked input, the same field silently
        goes missing from the save again.
        """
        import re
        from odoo.addons.kensei2.models.kensei2_tracker_allocation import (
            Kensei2TrackerAllocation)
        arch = self.Alloc.get_views([(False, "form")])["views"]["form"]["arch"]

        for fname in Kensei2TrackerAllocation._LOCKED_INPUT_FIELDS:
            # every EDITABLE occurrence (readonly=is_locked...) must force_save
            for tag in re.findall(
                    r'<field name="%s"[^>]*/>' % fname, arch):
                if "is_locked" not in tag:
                    continue          # a readonly="1" mirror — never dirty, never sent
                self.assertIn(
                    'force_save="1"', tag,
                    "%s can turn readonly mid-edit (is_locked) but is not "
                    "force_save — its value will be dropped from the save." % fname)

    def test_completing_stage_2_persists_through_the_form(self):
        """Drive the exact browser path: onchange -> web_save. The onchange is what
        flips is_locked; the web_save must still land."""
        s1 = self._make_alloc(suffix="formsave")
        self._complete_stage1(s1)
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": s1.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()
        s2 = self.Alloc.search([("task_id", "=", s1.task_id), ("stage_no", "=", 2)])

        payload = {
            "baseline_drive_link": "https://passitk.example.com",
            "baseline_gen_status": "done",
            "qced_by": self.env.user.id,
            "manual_qc_status": "done",
        }
        # 1) the client's onchange — this is what turns the fields readonly
        oc = s2.onchange({"id": s2.id, **payload},
                         ["manual_qc_status"],
                         {"status": {}, "is_locked": {}})
        self.assertEqual(oc["value"]["status"], "deliverable")
        self.assertTrue(oc["value"]["is_locked"],
                        "premise changed: is_locked no longer flips during onchange")

        # 2) the save must STILL persist, readonly-mid-edit or not
        s2.web_save(payload, {"status": {}})
        s2.invalidate_recordset()
        self.assertEqual(s2.status, "deliverable")
        self.assertEqual(s2.baseline_drive_link, "https://passitk.example.com")
        self.assertEqual(s2.manual_qc_status, "done")
        self.assertTrue(s2.date_final)


    def test_stage_1_says_baseline_and_stage_2_says_pass_it_k(self):
        """Each stage names the trajectory step its own way, on EVERY surface.

        Both stages run the same pipeline over the same fields and the SAME stored
        values (`ready_baseline`, `baseline_generated`) — but stage 1 calls the step
        Baseline and stage 2 calls it Pass It K. A Selection carries one set of
        labels, so `status` holds stage 1's and `stage2_status` holds stage 2's.

        The trap this guards: the list and kanban mix both stages, so rendering the
        raw `status` label showed "Baseline Generated" on a stage-2 row whose form
        said "Pass It K Generated" — the same task reading two different things.
        `status_label` picks the right one per row. If any surface goes back to the
        single Selection, this fails.
        """
        Alloc = self.env["kensei2.tracker.allocation"]
        s1 = dict(Alloc._fields["status"].selection)
        s2 = dict(Alloc._fields["stage2_status"].selection)

        # 1. the two dialects
        self.assertEqual(s1["ready_baseline"], "Ready for Baseline")
        self.assertEqual(s1["baseline_generated"], "Baseline Generated")
        self.assertEqual(s2["ready_baseline"], "Ready for Pass It K")
        self.assertEqual(s2["baseline_generated"], "Pass It K Generated")

        # 2. same STORED values — the labels differ, the data does not
        self.assertEqual(set(s1), set(s2))

        # 3. status_label picks per ROW (this is what the list and kanban render)
        a1 = self._make_alloc(suffix="lbl1")
        a1.write({"drive_link": "https://d/a", "pl_verified_status": "done",
                  "baseline_ready_status": "done"})
        a1.invalidate_recordset()
        self.assertEqual(a1.stage_no, 1)
        self.assertEqual(a1.status, "ready_baseline")
        self.assertEqual(a1.status_label, "Ready for Baseline",
                         "a stage-1 row must read Baseline")

        self._complete_stage1(a1)
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": a1.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()
        a2 = self.Alloc.search([("task_id", "=", a1.task_id), ("stage_no", "=", 2)])
        self.assertEqual(a2.status, "ready_baseline")           # same stored value…
        self.assertEqual(a2.status_label, "Ready for Pass It K",  # …different word
                         "a stage-2 row must read Pass It K")
        self.assertEqual(a2.stage2_status, "ready_baseline")

        # 4. the dashboard gives the two stages SEPARATE funnel cards. It used to
        #    carry one card that merely renamed itself with the stage filter, so
        #    with no filter (the default view) Pass It K was invisible and its
        #    tasks were counted as stage-1 trajectory work.
        cards = {c["key"]: c for c in tracker._funnel({(2, "ready_baseline"): 1})}
        self.assertEqual(cards["pass_it_k"]["value"], 1)
        self.assertEqual(cards["pass_it_k"]["label"], "Stage 2 Pass It K")
        self.assertEqual(cards["in_trajectory"]["value"], 0,
                         "a stage-2 task must not be counted as stage-1 trajectory")

    def test_cannot_hand_off_twice(self):
        alloc = self._ready_stage1()
        Wiz = self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql)
        Wiz.create({
            "allocation_id": alloc.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()

        alloc.invalidate_recordset()
        self.assertFalse(alloc.can_hand_off)
        with self.assertRaises(ValidationError):
            Wiz.create({
                "allocation_id": alloc.id,
                "tasker_member_id": self.member_other.id,
            }).action_confirm()

    def test_tasker_cannot_hand_off(self):
        alloc = self._ready_stage1()
        with self.assertRaises(AccessError):
            self.env["kensei2.tracker.stage.handoff"].with_user(
                self.user_tasker).create({
                    "allocation_id": alloc.id,
                    "tasker_member_id": self.member_other.id,
                }).action_confirm()

    def test_one_task_one_tasker_per_stage(self):
        """The DB constraint is (task_id, stage_no) — a stage can never be
        allocated twice, even under a race."""
        alloc = self._make_alloc(suffix="uniq")
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self._make_alloc(
                    suffix="uniq2", task_id=alloc.task_id, stage_no=1,
                    member=self.member_other)


class TestTrackerReopen(Kensei2TrackerCommon):

    def test_reopen_withdraws_delivery_credit(self):
        alloc = self._make_alloc(suffix="reopen", stage_no=2)
        alloc.write({
            "baseline_drive_link": "https://drive.example.com/b",
            "baseline_gen_status": "done",
            "qced_by": self.env.user.id,
            "manual_qc_status": "done",
        })
        self.assertEqual(alloc.status, "deliverable")
        self.assertTrue(alloc.date_final)

        alloc.with_user(self.user_ql).action_reopen()
        alloc.invalidate_recordset()
        self.assertNotEqual(alloc.status, "deliverable")
        self.assertFalse(alloc.is_locked)
        # the work is not finished any more, so the credit goes away
        self.assertFalse(alloc.date_final)

    def test_tasker_cannot_reopen(self):
        alloc = self._make_alloc(suffix="noreopen", stage_no=2)
        alloc.write({
            "baseline_drive_link": "https://drive.example.com/b",
            "baseline_gen_status": "done",
            "qced_by": self.env.user.id,
            "manual_qc_status": "done",
        })
        with self.assertRaises(AccessError):
            alloc.with_user(self.user_tasker).action_reopen()


class TestTrackerDashboardDataAccess(Kensei2TrackerCommon):
    """A lead must be able to load the Dashboard and Daily Tracker WITHOUT HR rights.

    pl_id / team_lead_id are hr.employee. A PL or QL is not an HR user, so
    hr.employee exposes only its *public profile* to them, and the ORM prefetches
    the whole row — so merely reading ``.name`` off a grouped employee raises
    AccessError and takes the entire request down. The symptom is the Daily
    Tracker showing "Failed to load Daily Tracker data." to every PL.

    controllers/tracker.py::_label reads those names with sudo. These tests drive
    the real controller helpers as a real PL, with no HR groups.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        alloc = cls._make_alloc(suffix="ctrl")
        alloc.write({"assigned_pl_id": cls.user_pl.id})
        # This DB auto-creates an hr.employee per res.users, so the PL already has
        # one (creating a second would trip the unique user_id constraint) — and
        # that is exactly what makes pl_id resolve to an employee the grouped read
        # then has to read a name off. Assert it rather than assume it: with no
        # employee, pl_id is empty and these tests would pass vacuously.
        cls.pl_employee = employee_for(cls.env, cls.user_pl)
        alloc.invalidate_recordset()
        cls.alloc_ctrl = alloc

    def test_pl_is_not_an_hr_user(self):
        """The premise. If this ever fails the other tests here prove nothing."""
        self.assertFalse(self.user_pl.has_group("hr.group_hr_user"))

    def test_the_allocation_actually_resolves_an_employee_pl(self):
        """Guards against the tests passing vacuously: if pl_id were empty there
        would be no hr.employee to read a name off, and _label would never be
        exercised."""
        self.alloc_ctrl.invalidate_recordset()
        self.assertTrue(
            self.pl_employee,
            "the PL has no hr.employee — these tests would prove nothing")
        self.assertEqual(self.alloc_ctrl.pl_id, self.pl_employee)

    def test_progress_rows_loads_for_a_pl(self):
        """The Dashboard's per-PL progress table groups by pl_id (hr.employee)."""
        from odoo.addons.kensei2.controllers.tracker import (
            _PROGRESS_GROUPS, _progress_rows)
        Alloc = self.Alloc.with_user(self.user_pl)
        for axis in ("pl", "ql", "tasker"):
            rows = _progress_rows(Alloc, _PROGRESS_GROUPS[axis], [])
            self.assertIsInstance(rows, list)

    def test_daily_roster_loads_for_a_pl(self):
        """The exact grouped read the Daily Tracker runs to build its roster."""
        from odoo.addons.kensei2.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        for _email, _name, pl, _count in Alloc._read_group(
                [], ["tasker_email", "tasker_name", "pl_id"], ["__count"]):
            self.assertIsInstance(_label(pl), str)

    def test_daily_filters_load_for_a_pl(self):
        from odoo.addons.kensei2.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        for pl, in Alloc._read_group([("pl_id", "!=", False)], ["pl_id"]):
            self.assertIsInstance(_label(pl), str)
        for lead, in Alloc._read_group(
                [("team_lead_id", "!=", False)], ["team_lead_id"]):
            self.assertIsInstance(_label(lead), str)

    def test_label_reads_the_employee_name_a_pl_cannot(self):
        """_label() must return a name where a direct read cannot.

        The restriction that makes _label necessary — hr.employee exposing only a
        public profile to a non-HR user — comes from the ETP add-ons
        (task_forge_bridge et al), not from kensei2 or from stock hr. So it exists
        on the real databases and NOT on a bare Odoo install.

        Assert what is true EITHER WAY: _label always gets the name. Then, only if
        the restriction is actually present, assert the direct read still raises —
        which is the thing that makes _label's sudo load-bearing rather than dead
        weight. Skipping that half on an unrestricted database keeps the test
        honest instead of failing it for the environment it happens to run in.
        """
        from odoo.addons.kensei2.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        groups = Alloc._read_group([("pl_id", "!=", False)], ["pl_id"])
        employees = [pl for pl, in groups if pl]
        if not employees:
            self.skipTest("no allocation with a resolved hr.employee PL")
        emp = employees[0]

        # the invariant the Daily Tracker depends on, on EVERY database
        self.assertTrue(_label(emp), "_label failed to read the employee's name")

        try:
            _ = emp.name
        except AccessError:
            return  # restricted, as on the ETP databases — _label is doing real work
        self.skipTest(
            "hr.employee is not field-restricted on this database (the ETP add-ons "
            "that restrict it are absent), so there is nothing for _label to defend "
            "against here")



class TestTrackerStageAwareDashboard(Kensei2TrackerCommon):
    """The dashboard must not merge Baseline (stage 1) with Pass It K (stage 2).

    Both stages store the SAME status values -- 'ready_baseline' and
    'baseline_generated' -- because they run the same two rungs; only the NAMES
    differ. Every dashboard surface used to bucket on the status alone, so a stage-2
    task in Pass It K was counted and displayed as stage-1 "In Trajectory", and Pass
    It K had no card and no column of its own anywhere.
    """

    def test_the_two_stages_share_the_stored_status(self):
        """The premise. If this ever stops holding, the tests below prove nothing."""
        s1 = self._make_alloc(suffix="sd-premise-1", stage_no=1)
        self._complete_stage1_to_baseline(s1)
        s2 = self._make_alloc(suffix="sd-premise-2", stage_no=2)
        s2.write({"baseline_drive_link": "https://drive.example.com/pik"})
        self.assertEqual(s1.status, "baseline_generated")
        self.assertEqual(s2.status, s1.status)
        # ...and yet they are DIFFERENT work, which only the label reveals.
        self.assertEqual(s1.status_label, "Baseline Generated")
        self.assertEqual(s2.status_label, "Pass It K Generated")

    def test_baseline_and_pass_it_k_get_their_own_funnel_cards(self):
        cards = {c["key"]: c for c in tracker._funnel({
            (1, "baseline_generated"): 3,
            (2, "baseline_generated"): 5,
        })}
        self.assertEqual(cards["in_trajectory"]["value"], 3,
                         "stage 2's Pass It K leaked into stage 1's In Trajectory")
        self.assertEqual(cards["pass_it_k"]["value"], 5,
                         "Pass It K has no card of its own")
        self.assertEqual(cards["pass_it_k"]["label"], "Stage 2 Pass It K")

    def test_funnel_card_drilldown_carries_its_stage(self):
        """Both cards count the same statuses, so the stage is the ONLY thing that
        keeps their drill-downs apart. Without it, clicking either opens both."""
        cards = {c["key"]: c for c in tracker._funnel({})}
        self.assertEqual(cards["in_trajectory"]["stage"], 1)
        self.assertEqual(cards["pass_it_k"]["stage"], 2)
        self.assertEqual(cards["in_trajectory"]["statuses"],
                         cards["pass_it_k"]["statuses"])
        # Manual QC is each stage's OWN gate and stores the same value on both, so it
        # splits for exactly the same reason the trajectory step does.
        self.assertEqual(cards["manual_qc_s1"]["stage"], 1)
        self.assertEqual(cards["manual_qc_s2"]["stage"], 2)
        self.assertEqual(cards["manual_qc_s1"]["statuses"],
                         cards["manual_qc_s2"]["statuses"])
        # A step that genuinely belongs to no single stage stays stage-less.
        self.assertIsNone(cards["failed"]["stage"])

    def test_progress_columns_split_by_stage(self):
        self.assertEqual(tracker._bucket_for(1, "baseline_generated"), "in_traj")
        self.assertEqual(tracker._bucket_for(2, "baseline_generated"), "pass_it_k")
        self.assertEqual(tracker._bucket_for(1, "ready_baseline"), "ready")
        self.assertEqual(tracker._bucket_for(2, "ready_baseline"), "pik_ready")
        # each stage QCs its own work, in its own column
        self.assertEqual(tracker._bucket_for(1, "manual_qc"), "s1_qc")
        self.assertEqual(tracker._bucket_for(2, "manual_qc"), "s2_qc")
        # genuinely shared rungs land in one column from either stage
        for stage in (1, 2):
            self.assertEqual(tracker._bucket_for(stage, "deliverable"), "verified")
            self.assertEqual(tracker._bucket_for(stage, "failed"), "blocked")

    def test_every_reachable_status_lands_in_exactly_one_column_and_one_card(self):
        """No row may fall between the buckets.

        A (stage, status) with no column is still counted in the row's Total, so its
        cells would silently fail to add up to their own total -- a task visible in
        the total and present nowhere else on the table.
        """
        s1 = [k for k, _l in self.Alloc.STATUS_SELECTION]
        s2 = [k for k, _l in self.Alloc.STAGE2_STATUS_SELECTION]
        # stage 3+ runs the stage-2 ladder (see _compute_status), so it must bucket too
        for stage, statuses in ((1, s1), (2, s2), (3, s2)):
            for status in statuses:
                col = tracker._bucket_for(stage, status)
                self.assertIn(col, tracker._PROGRESS_COLUMNS,
                              "stage %s / %s has no progress column" % (stage, status))
                cards = [c for c in tracker._funnel({(stage, status): 1})
                         if c["value"]]
                self.assertEqual(
                    len(cards), 1,
                    "stage %s / %s landed in %d funnel cards, want exactly 1"
                    % (stage, status, len(cards)))

    def test_progress_rows_report_pass_it_k_separately(self):
        """End to end, through the real endpoint payload."""
        s1 = self._make_alloc(suffix="sd-row-1", stage_no=1)
        self._complete_stage1_to_baseline(s1)
        s2 = self._make_alloc(suffix="sd-row-2", stage_no=2)
        s2.write({"baseline_drive_link": "https://drive.example.com/pik"})

        rows = tracker._progress_rows(
            self.Alloc.sudo(), "pl_id", [("id", "in", (s1 | s2).ids)])
        row = next(r for r in rows if r["total"] == 2)
        self.assertEqual(row["in_traj"], 1, "stage 1 Baseline")
        self.assertEqual(row["pass_it_k"], 1, "stage 2 Pass It K")
        self.assertEqual(
            sum(row[c] for c in tracker._PROGRESS_COLUMNS), row["total"],
            "the columns must account for every task in Total")

    def test_each_stage_names_its_own_manual_qc(self):
        """The two QC gates share a stored value but are different queues."""
        s1 = self._make_alloc(suffix="sd-qc-1", stage_no=1)
        self._complete_stage1_to_baseline(s1)
        s1.write({"baseline_gen_status": "done", "qced_by": self.env.user.id})
        s2 = self._make_alloc(suffix="sd-qc-2", stage_no=2)
        s2.write({"baseline_drive_link": "https://drive.example.com/pik",
                  "baseline_gen_status": "done", "qced_by": self.env.user.id})

        self.assertEqual(s1.status, "manual_qc")
        self.assertEqual(s2.status, "manual_qc")          # same stored value...
        self.assertEqual(s1.status_label, "Stage 1 Manual QC")   # ...different queue
        self.assertEqual(s2.status_label, "Stage 2 Manual QC")

    def test_stage_1_never_delivers_and_stage_2_never_hands_off(self):
        """The two ladders have different terminal steps.

        Stage 1 ends at Ready for Next Stage -- it can never reach Deliverable,
        because only the FINAL stage delivers. Stage 2, being final, ends at
        Deliverable and never hands off.
        """
        s1 = self._make_alloc(suffix="sd-term-1", stage_no=1)
        self._complete_stage1(s1)
        self.assertEqual(s1.status, "ready_next_stage")
        self.assertNotEqual(s1.status, "deliverable")

        s2 = self._make_alloc(suffix="sd-term-2", stage_no=2)
        s2.write({"baseline_drive_link": "https://drive.example.com/pik",
                  "baseline_gen_status": "done", "qced_by": self.env.user.id,
                  "manual_qc_status": "done"})
        self.assertTrue(s2.is_final_stage)
        self.assertEqual(s2.status, "deliverable")
        self.assertNotEqual(s2.status, "ready_next_stage")

    def test_daily_status_filter_names_both_ladders(self):
        """The filter matches the stored status, which spans both stages -- so an
        option labelled only 'Baseline Generated' silently returns Pass It K rows."""
        labels = dict(self.Alloc.STATUS_SELECTION)
        s2 = dict(self.Alloc.STAGE2_STATUS_SELECTION)
        ambiguous = [k for k in labels if k in s2 and s2[k] != labels[k]]
        self.assertEqual(sorted(ambiguous),
                         ["baseline_generated", "manual_qc", "ready_baseline"])

    @staticmethod
    def _complete_stage1_to_baseline(alloc):
        alloc.write({
            "drive_link": "https://drive.example.com/a",
            "pl_verified_status": "done",
            "baseline_ready_status": "done",
            "baseline_drive_link": "https://drive.example.com/b",
        })


class TestTrackerPersona(Kensei2TrackerCommon):
    """The tracker fields projected onto kensei2.persona."""

    def test_action_view_allocations_builds_an_action(self):
        """Regression: the ported persona used _() without importing it, so this
        button died with `NameError: name '_' is not defined` the moment anyone
        clicked it. Calling the method IS the test — an unimported name is only
        caught at runtime."""
        persona = self.Persona.create({"name": "persona-action-probe"})
        action = persona.action_view_allocations()
        self.assertEqual(action["res_model"], "kensei2.tracker.allocation")
        self.assertIn(("persona_id", "=", persona.id), action["domain"])
        self.assertTrue(action["name"])

    def test_assignment_status_tracks_its_allocations(self):
        """assignment_status / allocation_count are STORED computes. If they do not
        follow the allocations, a persona shows "Assigned" with nothing assigned to
        it — which is exactly what a stale stored value looks like."""
        persona = self.Persona.create({"name": "persona-status-probe"})
        self.assertEqual(persona.assignment_status, "unassigned")
        self.assertEqual(persona.allocation_count, 0)

        alloc = self._make_alloc(suffix="persona-status", persona_id=persona.id)
        persona.invalidate_recordset()
        self.assertEqual(persona.assignment_status, "assigned")
        self.assertEqual(persona.allocation_count, 1)
        self.assertEqual(persona.current_allocation_id, alloc)

        alloc.unlink()
        persona.invalidate_recordset()
        self.assertEqual(persona.assignment_status, "unassigned",
                         "persona still reads 'assigned' with no allocations")
        self.assertEqual(persona.allocation_count, 0)
        self.assertFalse(persona.current_allocation_id)


class TestTrackerListStats(Kensei2TrackerCommon):
    """The stat cards above the Task Allocation / Personas / Team list views."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.a1 = cls._make_alloc(suffix="ls1")
        cls.a1.write({"assigned_pl_id": cls.user_pl.id})
        cls.a2 = cls._make_alloc(suffix="ls2", member=cls.member_other,
                                 persona_id=cls.persona2.id)
        cls.a2.write({"assigned_pl_id": cls.user_pl2.id})  # a RIVAL lead's task
        cls._complete_stage1(cls.a1)                       # -> ready_next_stage

    def _cards(self, model, domain=None, user=None):
        from odoo.addons.kensei2.controllers.tracker import _LIST_STATS
        env = self.env(user=user) if user else self.env
        builder = _LIST_STATS[model]
        return {c["key"]: c["value"]
                for c in builder(env[model], domain or [])}

    def test_allocation_cards(self):
        cards = self._cards("kensei2.tracker.allocation")
        self.assertEqual(cards["total"],
                         cards["active"] + cards["completed"] + cards["failed"],
                         "the buckets must add up to the total")
        self.assertIn("avg_score", cards)

    def test_persona_cards(self):
        cards = self._cards("kensei2.persona")
        self.assertEqual(cards["total"],
                         cards["assigned"] + cards["unassigned"],
                         "assigned + unassigned must equal the total")

    def test_team_cards(self):
        cards = self._cards("kensei2.tracker.team.member")
        self.assertGreaterEqual(cards["total"], 2)

    def test_only_narrowing_cells_are_clickable(self):
        """A cell that cannot filter must carry no domain, so it renders as an
        inert <div> instead of a button that looks clickable and does nothing."""
        from odoo.addons.kensei2.controllers.tracker import _LIST_STATS
        for model, builder in _LIST_STATS.items():
            by_key = {c["key"]: c for c in builder(self.env[model], [])}
            self.assertFalse(
                by_key["total"]["domain"],
                "%s: the total cannot narrow anything and must not be clickable"
                % model)
            for key, card in by_key.items():
                if key in ("total", "avg_score"):
                    continue
                self.assertTrue(
                    card["domain"],
                    "%s: the %s cell should filter the list" % (model, key))

    def test_shares_are_fractions_of_the_total(self):
        """The proportion bar is only meaningful if share == value / total."""
        from odoo.addons.kensei2.controllers.tracker import _LIST_STATS
        for model, builder in _LIST_STATS.items():
            cards = builder(self.env[model], [])
            total = next(c["value"] for c in cards if c["key"] == "total")
            for card in cards:
                if card["share"] is None:
                    continue
                expected = (card["value"] / total) if total else 0.0
                self.assertAlmostEqual(
                    card["share"], expected, places=6,
                    msg="%s/%s: share does not match value/total"
                        % (model, card["key"]))
                self.assertGreaterEqual(card["share"], 0.0)
                self.assertLessEqual(card["share"], 1.0)

    def test_an_average_carries_no_share(self):
        """Avg Score is not a subset of the total; a bar would imply a proportion
        that does not exist."""
        cards = {c["key"]: c
                 for c in self._cards_raw("kensei2.tracker.allocation")}
        self.assertIsNone(cards["avg_score"]["share"])
        self.assertIsNone(cards["total"]["share"])

    def _cards_raw(self, model, domain=None):
        from odoo.addons.kensei2.controllers.tracker import _LIST_STATS
        return _LIST_STATS[model](self.env[model], domain or [])

    def test_cards_follow_the_search_domain(self):
        """Numbers that contradict the rows beneath them are worse than none, so
        the cards must be computed against the list's CURRENT domain."""
        everything = self._cards("kensei2.tracker.allocation")
        filtered = self._cards(
            "kensei2.tracker.allocation", [("task_id", "=", self.a1.task_id)])
        self.assertEqual(filtered["total"], 1)
        self.assertLess(filtered["total"], everything["total"])

    def test_cards_are_scoped_by_the_record_rules(self):
        """A PL's cards must count their OWN TEAM, not the whole org — otherwise
        the stats leak exactly what the three-tier rules exist to hide."""
        as_pl = self._cards("kensei2.tracker.allocation", user=self.user_pl)
        as_admin = self._cards("kensei2.tracker.allocation", user=self.user_admin)
        # PL1 owns a1; a2 belongs to the rival PL2 and must not be counted
        self.assertEqual(as_pl["total"], 1)
        self.assertGreater(as_admin["total"], as_pl["total"])

    def test_unknown_model_yields_no_cards(self):
        """The model name comes from the client, so it is whitelisted rather than
        handed to request.env — otherwise the route would aggregate any table."""
        from odoo.addons.kensei2.controllers.tracker import _LIST_STATS
        self.assertNotIn("res.users", _LIST_STATS)
        self.assertNotIn("res.partner", _LIST_STATS)
        self.assertEqual(set(_LIST_STATS), {
            "kensei2.tracker.allocation",
            "kensei2.persona",
            "kensei2.tracker.team.member",
        })


@tagged("post_install", "-at_install")
class TestTrackerListStatsEndpoint(HttpCase):
    """The stats route over real HTTP — a tasker must not get org-wide numbers."""

    def test_unknown_model_returns_no_cards(self):
        self.authenticate("admin", "admin")
        resp = self.url_open(
            "/kensei2/tracker/list_stats",
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": {"model": "res.users", "domain": []}}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("error", body)
        self.assertEqual(body["result"]["cards"], [],
                         "a non-whitelisted model must yield no cards")

    def test_allocation_stats_load(self):
        self.authenticate("admin", "admin")
        resp = self.url_open(
            "/kensei2/tracker/list_stats",
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": {"model": "kensei2.tracker.allocation",
                                        "domain": []}}),
            headers={"Content-Type": "application/json"},
        )
        body = resp.json()
        self.assertNotIn("error", body)
        keys = {c["key"] for c in body["result"]["cards"]}
        self.assertEqual(
            keys, {"total", "active", "completed", "failed", "avg_score"})


@tagged("post_install", "-at_install")
class TestTrackerDailyEndpoint(HttpCase):
    """Drive the Daily Tracker routes over real HTTP, logged in as a real PL.

    This is the test that would have caught the live bug. The unit tests above
    exercise the controller's HELPERS; only this one exercises the ROUTE, so only
    this one fails if someone reintroduces a bare ``pl.name`` read anywhere in it.

    The bug: pl_id / team_lead_id are hr.employee. A PL is not an HR user, so
    hr.employee gives them the public profile only, and reading .name prefetches
    the whole row and raises AccessError. Odoo returns that inside a JSON-RPC error
    envelope with HTTP **200**, so the route "succeeds" at the HTTP layer while the
    OWL client throws and shows "Failed to load Daily Tracker data." — which is why
    asserting on the status code alone is not enough. Assert on the payload.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pl_user = cls.env["res.users"].with_context(
            etp_importing=True).create({
                "name": "daily.pl",
                "login": "daily.pl@ethara.ai",
                "email": "daily.pl@ethara.ai",
                "password": "daily.pl.pw",
                "group_ids": [(6, 0, [
                    cls.env.ref("base.group_user").id,
                    cls.env.ref("kensei2.group_kensei2_pl").id,
                ])],
            })
        tasker = cls.env["res.users"].with_context(etp_importing=True).create({
            "name": "daily.tasker",
            "login": "daily.tasker@ethara.ai",
            "email": "daily.tasker@ethara.ai",
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("kensei2.group_kensei2_tasker").id,
            ])],
        })
        member = cls.env["kensei2.tracker.team.member"].with_context(
            kensei2_skip_toast=True).create({
                "user_id": tasker.id,
                "status": "active",
                "assigned_pl_id": cls.pl_user.id,
            })
        persona = cls.env["kensei2.persona"].create({"name": "daily-endpoint-persona"})
        cls.alloc = cls.env["kensei2.tracker.allocation"].create({
            "task_id": str(uuid.uuid4()),
            "persona_id": persona.id,
            "tasker_member_id": member.id,
        })
        # The PL must resolve to an hr.employee, or pl_id is empty, no employee is
        # ever grouped, and the AccessError this test exists to catch never fires.
        cls.pl_employee = employee_for(cls.env, cls.pl_user)

    def _call(self, route, params=None):
        """POST a JSON-RPC call and return the parsed body."""
        resp = self.url_open(
            route,
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "params": params or {},
            }),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_premise_pl_has_an_employee_and_no_hr_rights(self):
        """Without these, the two tests below would pass vacuously."""
        self.assertTrue(
            self.pl_employee, "the PL has no hr.employee to trip the restriction")
        self.assertFalse(self.pl_user.has_group("hr.group_hr_user"))
        self.alloc.invalidate_recordset()
        self.assertEqual(self.alloc.pl_id, self.pl_employee)

    def test_daily_data_loads_for_a_pl(self):
        self.authenticate("daily.pl@ethara.ai", "daily.pl.pw")
        body = self._call("/kensei2/tracker/daily/data")
        self.assertNotIn(
            "error", body,
            "Daily Tracker data failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])
        self.assertIn("rows", body.get("result", {}))

    def test_daily_filters_load_for_a_pl(self):
        self.authenticate("daily.pl@ethara.ai", "daily.pl.pw")
        body = self._call("/kensei2/tracker/daily/filters")
        self.assertNotIn(
            "error", body,
            "Daily Tracker filters failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])
        self.assertIn("pls", body.get("result", {}))

    def test_dashboard_loads_for_a_pl(self):
        """Same trap: the Dashboard's progress table groups by pl_id."""
        self.authenticate("daily.pl@ethara.ai", "daily.pl.pw")
        body = self._call("/kensei2/tracker/dashboard")
        self.assertNotIn(
            "error", body,
            "Dashboard failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])


class TestTrackerReset(Kensei2TrackerCommon):
    """action_reset_task — throw the work away and start the whole task over.

    Distinct from action_reopen, which un-freezes ONE stage and KEEPS its work.
    """

    def _task_with_two_stages(self):
        """A fully-worked stage 1, handed off to a live stage 2."""
        alloc = self._make_alloc(suffix="reset")
        self._complete_stage1(alloc)
        self.env["kensei2.tracker.stage.handoff"].with_user(self.user_ql).create({
            "allocation_id": alloc.id,
            "tasker_member_id": self.member_other.id,
        }).action_confirm()
        alloc.invalidate_recordset()
        return alloc

    def test_reset_values_cover_every_stage_input(self):
        """Drift guard: the reset must clear EVERY field the lock protects.

        _LOCKED_INPUT_FIELDS is the definition of "a stage's work". If someone adds
        a field there and forgets _RESET_VALUES, reset would silently leave that
        field populated on a supposedly-clean task.
        """
        Alloc = self.env["kensei2.tracker.allocation"]
        self.assertEqual(
            set(Alloc._RESET_VALUES), set(Alloc._LOCKED_INPUT_FIELDS),
            "_RESET_VALUES and _LOCKED_INPUT_FIELDS have drifted apart")

    def test_reset_wipes_work_and_deletes_later_stages(self):
        alloc = self._task_with_two_stages()
        stage2 = self.Alloc.search([
            ("task_id", "=", alloc.task_id), ("stage_no", "=", 2)])
        self.assertTrue(stage2)

        alloc.with_user(self.user_ql).action_reset_task()
        alloc.invalidate_recordset()

        # stage 2 is gone
        self.assertFalse(stage2.exists(), "the later stage was not deleted")
        # every stage input is cleared
        for field, expected in self.Alloc._RESET_VALUES.items():
            actual = alloc[field]
            if hasattr(actual, "id"):        # m2o -> empty recordset
                self.assertFalse(actual, "%s survived the reset" % field)
            else:
                self.assertEqual(actual, expected,
                                 "%s survived the reset" % field)
        # and the task is back at the start of the pipeline
        self.assertEqual(alloc.status, "in_progress")
        self.assertEqual(alloc.final_status, "in_progress")
        # the delivery credit the Daily Tracker counted is withdrawn
        self.assertFalse(alloc.date_final)
        self.assertFalse(alloc.is_locked)

    def test_reset_keeps_the_task_identity(self):
        """Wipes the WORK, not the assignment."""
        alloc = self._task_with_two_stages()
        before = {
            "task_id": alloc.task_id,
            "persona": alloc.persona_id,
            "tasker": alloc.tasker_member_id,
            "pl": alloc.assigned_pl_id,
            "ql": alloc.assigned_ql_id,
            "date": alloc.assigned_date,
        }
        alloc.with_user(self.user_ql).action_reset_task()
        alloc.invalidate_recordset()

        self.assertEqual(alloc.task_id, before["task_id"])
        self.assertEqual(alloc.persona_id, before["persona"])
        self.assertEqual(alloc.tasker_member_id, before["tasker"])
        self.assertEqual(alloc.assigned_pl_id, before["pl"])
        self.assertEqual(alloc.assigned_ql_id, before["ql"])
        self.assertEqual(alloc.assigned_date, before["date"])

    def test_reset_from_stage_2_lands_on_the_survivor(self):
        """Resetting FROM stage 2 deletes the very record you were looking at, so
        the action must redirect to stage 1 rather than a dead res_id."""
        alloc = self._task_with_two_stages()
        stage2 = self.Alloc.search([
            ("task_id", "=", alloc.task_id), ("stage_no", "=", 2)])

        action = stage2.with_user(self.user_ql).action_reset_task()

        self.assertFalse(stage2.exists())
        self.assertEqual(action["res_id"], alloc.id,
                         "reset from stage 2 must land on stage 1")
        self.assertTrue(alloc.exists())

    def test_reset_is_chatter_logged(self):
        alloc = self._task_with_two_stages()
        before = len(alloc.message_ids)
        alloc.with_user(self.user_ql).action_reset_task()
        alloc.invalidate_recordset()
        self.assertGreater(len(alloc.message_ids), before,
                           "the reset was not logged to the chatter")

    def test_reset_on_a_single_stage_task(self):
        """No later stages to delete — must still clear the work."""
        alloc = self._make_alloc(suffix="reset1")
        alloc.write({"drive_link": "https://drive.example.com/a",
                     "tasker_qc_notes": "some work"})
        self.assertEqual(alloc.status, "tasker_qc_completed")

        alloc.with_user(self.user_ql).action_reset_task()
        alloc.invalidate_recordset()
        self.assertFalse(alloc.drive_link)
        self.assertFalse(alloc.tasker_qc_notes)
        self.assertEqual(alloc.status, "in_progress")

    def test_tasker_cannot_reset(self):
        alloc = self._make_alloc(suffix="reset-deny")
        alloc.write({"drive_link": "https://drive.example.com/a"})
        with self.assertRaises(AccessError):
            alloc.with_user(self.user_tasker).action_reset_task()
        alloc.invalidate_recordset()
        self.assertEqual(alloc.drive_link, "https://drive.example.com/a",
                         "a tasker's failed reset still wiped the work")


class TestTrackerBulkAllocation(Kensei2TrackerCommon):

    def test_round_robin_respects_per_tasker_cap(self):
        Wiz = self.env["kensei2.tracker.bulk.allocation"]
        personas = self.Persona.create(
            [{"name": "bulk-p-%s" % i} for i in range(5)])

        wizard = Wiz.create({
            "source_mode": "unassigned",
            "allocation_method": "sequential",
            "limit_mode": "limited",
            "allocation_limit": 2,
            "tasker_line_ids": [
                (0, 0, {"member_id": self.member_tasker.id, "selected": True,
                        "current_count": 0}),
                (0, 0, {"member_id": self.member_other.id, "selected": True,
                        "current_count": 0}),
            ],
        })
        wizard.action_start()

        # 2 taskers x cap 2 = 4 placeable; the pool has at least 5 personas, so at
        # least one must come back unallocated rather than breaching the cap.
        self.assertEqual(wizard.assigned_count, 4)
        self.assertGreaterEqual(wizard.unallocated_count, 1)
        for member in (self.member_tasker, self.member_other):
            self.assertEqual(
                self.Alloc.search_count([("tasker_member_id", "=", member.id)]), 2)
        del personas  # created for the pool; assertions are on the counts

    def test_unassigned_domain_uses_the_stored_index(self):
        """PERF-2: the domain must be a constant-size indexed predicate, not a
        five-figure NOT IN list rebuilt from every allocation on every open."""
        domain = self.env["kensei2.tracker.bulk.allocation"]._unassigned_persona_domain()
        self.assertEqual(domain, [("assignment_status", "=", "unassigned")])

        fresh = self.Persona.create({"name": "bulk-unassigned-probe"})
        self.assertEqual(fresh.assignment_status, "unassigned")
        self._make_alloc(suffix="probe", persona_id=fresh.id)
        fresh.invalidate_recordset()
        self.assertEqual(fresh.assignment_status, "assigned")
