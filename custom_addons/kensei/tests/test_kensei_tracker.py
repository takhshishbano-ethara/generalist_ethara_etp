# -*- coding: utf-8 -*-
"""Kensei Tracker — allocation pipeline, access control and stage hand-off.

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

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase


@tagged("post_install", "-at_install")
class KenseiTrackerCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Alloc = cls.env["kensei.tracker.allocation"]
        cls.Member = cls.env["kensei.tracker.team.member"]
        cls.Persona = cls.env["kensei.persona"]

        cls.persona = cls.Persona.create({"name": "tracker-test-persona"})
        cls.persona2 = cls.Persona.create({"name": "tracker-test-persona-2"})

        cls.g_tasker = cls.env.ref("kensei.group_kensei_tasker")
        cls.g_ql = cls.env.ref("kensei.group_kensei_ql")
        cls.g_pl = cls.env.ref("kensei.group_kensei_pl")
        cls.g_admin = cls.env.ref("kensei.group_kensei_admin")
        cls.g_etp_tasker = cls.env.ref("etp_user_roles.group_tasker")

        cls.user_tasker = cls._make_user("kt.tasker", cls.g_tasker)
        cls.user_other = cls._make_user("kt.other", cls.g_tasker)
        cls.user_ql = cls._make_user("kt.ql", cls.g_ql)
        # Two rival leads, so "team" scoping is actually provable: each must see
        # their own team's allocations and NOT the other's.
        cls.user_pl = cls._make_user("kt.pl", cls.g_pl)
        cls.user_pl2 = cls._make_user("kt.pl2", cls.g_pl)
        cls.user_admin = cls._make_user("kt.admin", cls.g_admin)
        # An ETP-ladder user holding NO kensei.* group. This is the population the
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
          production rule but irrelevant here (we are testing Kensei's record
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
        return cls.Member.with_context(kensei_skip_toast=True).create(vals)

    @classmethod
    def _make_alloc(cls, member=None, **overrides):
        vals = {
            "task_id": "TSK-%s" % (overrides.pop("suffix", "001")),
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


class TestTrackerAccessControl(KenseiTrackerCommon):
    """Regression tests for each historical bypass of the privileged-field guard."""

    def test_etp_only_user_cannot_see_other_taskers(self):
        """SEC-1: an ETP-ladder user with no kensei.* group must NOT see everything.

        ir.model.access.csv grants etp_user_roles.group_tasker access to this model,
        but no record rule used to bind to that group. Odoo skips rules whose groups
        don't intersect the user's, then ANDs the globals with the OR of what
        survives — so an empty rule set collapses to TRUE. Result: every ETP role,
        up the whole chain to CFO, had UNRESTRICTED read/write on every allocation.
        """
        mine = self._make_alloc(suffix="etp-mine")
        # sanity: the ETP user really does hold no kensei group
        self.assertFalse(
            self.user_etp.has_group("kensei.group_kensei_tasker"),
            "fixture is wrong — the ETP user must hold no kensei.* group",
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
        """SEC-4: `kensei_reopen` was honoured on the context key alone.

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
                kensei_reopen=True).write({"rubric_score": 99.0})

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


class TestTrackerVisibilityTiers(KenseiTrackerCommon):
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
        """Guards a trap: group_kensei_admin IMPLIES group_kensei_pl, so without a
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


class TestTrackerStatusLadder(KenseiTrackerCommon):
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

    def test_task_id_must_be_alphanumeric(self):
        with self.assertRaises(ValidationError):
            self._make_alloc(suffix="x", task_id="has spaces!")


class TestTrackerStageHandoff(KenseiTrackerCommon):

    def _ready_stage1(self):
        alloc = self._make_alloc(suffix="handoff")
        self._complete_stage1(alloc)
        return alloc

    def test_handoff_creates_next_stage_clean(self):
        alloc = self._ready_stage1()
        self.assertTrue(alloc.can_hand_off)

        wizard = self.env["kensei.tracker.stage.handoff"].with_user(
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
        self.env["kensei.tracker.stage.handoff"].with_user(self.user_ql).create({
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

    def test_cannot_hand_off_twice(self):
        alloc = self._ready_stage1()
        Wiz = self.env["kensei.tracker.stage.handoff"].with_user(self.user_ql)
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
            self.env["kensei.tracker.stage.handoff"].with_user(
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


class TestTrackerReopen(KenseiTrackerCommon):

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


class TestTrackerDashboardDataAccess(KenseiTrackerCommon):
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
        cls.pl_employee = cls.env["hr.employee"].sudo().search(
            [("user_id", "=", cls.user_pl.id)], limit=1)
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
        from odoo.addons.kensei.controllers.tracker import (
            _PROGRESS_GROUPS, _progress_rows)
        Alloc = self.Alloc.with_user(self.user_pl)
        for axis in ("pl", "ql", "tasker"):
            rows = _progress_rows(Alloc, _PROGRESS_GROUPS[axis], [])
            self.assertIsInstance(rows, list)

    def test_daily_roster_loads_for_a_pl(self):
        """The exact grouped read the Daily Tracker runs to build its roster."""
        from odoo.addons.kensei.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        for _email, _name, pl, _count in Alloc._read_group(
                [], ["tasker_email", "tasker_name", "pl_id"], ["__count"]):
            self.assertIsInstance(_label(pl), str)

    def test_daily_filters_load_for_a_pl(self):
        from odoo.addons.kensei.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        for pl, in Alloc._read_group([("pl_id", "!=", False)], ["pl_id"]):
            self.assertIsInstance(_label(pl), str)
        for lead, in Alloc._read_group(
                [("team_lead_id", "!=", False)], ["team_lead_id"]):
            self.assertIsInstance(_label(lead), str)

    def test_reading_the_employee_name_direct_still_raises(self):
        """Guards the premise from the other side: if hr.employee ever stops
        restricting its fields, _label's sudo becomes dead weight and someone
        should notice — but until then, the direct read MUST raise, which is
        exactly why _label exists."""
        from odoo.addons.kensei.controllers.tracker import _label
        Alloc = self.Alloc.with_user(self.user_pl)
        groups = Alloc._read_group([("pl_id", "!=", False)], ["pl_id"])
        employees = [pl for pl, in groups if pl]
        if not employees:
            self.skipTest("no allocation with a resolved hr.employee PL")
        with self.assertRaises(AccessError):
            _ = employees[0].name
        self.assertTrue(_label(employees[0]))  # ...and sudo gets through


class TestTrackerListStats(KenseiTrackerCommon):
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
        from odoo.addons.kensei.controllers.tracker import _LIST_STATS
        env = self.env(user=user) if user else self.env
        builder = _LIST_STATS[model]
        return {c["key"]: c["value"]
                for c in builder(env[model], domain or [])}

    def test_allocation_cards(self):
        cards = self._cards("kensei.tracker.allocation")
        self.assertEqual(cards["total"],
                         cards["active"] + cards["completed"] + cards["failed"],
                         "the buckets must add up to the total")
        self.assertIn("avg_score", cards)

    def test_persona_cards(self):
        cards = self._cards("kensei.persona")
        self.assertEqual(cards["total"],
                         cards["assigned"] + cards["unassigned"],
                         "assigned + unassigned must equal the total")

    def test_team_cards(self):
        cards = self._cards("kensei.tracker.team.member")
        self.assertGreaterEqual(cards["total"], 2)

    def test_only_narrowing_cells_are_clickable(self):
        """A cell that cannot filter must carry no domain, so it renders as an
        inert <div> instead of a button that looks clickable and does nothing."""
        from odoo.addons.kensei.controllers.tracker import _LIST_STATS
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
        from odoo.addons.kensei.controllers.tracker import _LIST_STATS
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
                 for c in self._cards_raw("kensei.tracker.allocation")}
        self.assertIsNone(cards["avg_score"]["share"])
        self.assertIsNone(cards["total"]["share"])

    def _cards_raw(self, model, domain=None):
        from odoo.addons.kensei.controllers.tracker import _LIST_STATS
        return _LIST_STATS[model](self.env[model], domain or [])

    def test_cards_follow_the_search_domain(self):
        """Numbers that contradict the rows beneath them are worse than none, so
        the cards must be computed against the list's CURRENT domain."""
        everything = self._cards("kensei.tracker.allocation")
        filtered = self._cards(
            "kensei.tracker.allocation", [("task_id", "=", self.a1.task_id)])
        self.assertEqual(filtered["total"], 1)
        self.assertLess(filtered["total"], everything["total"])

    def test_cards_are_scoped_by_the_record_rules(self):
        """A PL's cards must count their OWN TEAM, not the whole org — otherwise
        the stats leak exactly what the three-tier rules exist to hide."""
        as_pl = self._cards("kensei.tracker.allocation", user=self.user_pl)
        as_admin = self._cards("kensei.tracker.allocation", user=self.user_admin)
        # PL1 owns a1; a2 belongs to the rival PL2 and must not be counted
        self.assertEqual(as_pl["total"], 1)
        self.assertGreater(as_admin["total"], as_pl["total"])

    def test_unknown_model_yields_no_cards(self):
        """The model name comes from the client, so it is whitelisted rather than
        handed to request.env — otherwise the route would aggregate any table."""
        from odoo.addons.kensei.controllers.tracker import _LIST_STATS
        self.assertNotIn("res.users", _LIST_STATS)
        self.assertNotIn("res.partner", _LIST_STATS)
        self.assertEqual(set(_LIST_STATS), {
            "kensei.tracker.allocation",
            "kensei.persona",
            "kensei.tracker.team.member",
        })


@tagged("post_install", "-at_install")
class TestTrackerListStatsEndpoint(HttpCase):
    """The stats route over real HTTP — a tasker must not get org-wide numbers."""

    def test_unknown_model_returns_no_cards(self):
        self.authenticate("admin", "admin")
        resp = self.url_open(
            "/kensei/tracker/list_stats",
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
            "/kensei/tracker/list_stats",
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": {"model": "kensei.tracker.allocation",
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
                    cls.env.ref("kensei.group_kensei_pl").id,
                ])],
            })
        tasker = cls.env["res.users"].with_context(etp_importing=True).create({
            "name": "daily.tasker",
            "login": "daily.tasker@ethara.ai",
            "email": "daily.tasker@ethara.ai",
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("kensei.group_kensei_tasker").id,
            ])],
        })
        member = cls.env["kensei.tracker.team.member"].with_context(
            kensei_skip_toast=True).create({
                "user_id": tasker.id,
                "status": "active",
                "assigned_pl_id": cls.pl_user.id,
            })
        persona = cls.env["kensei.persona"].create({"name": "daily-endpoint-persona"})
        cls.alloc = cls.env["kensei.tracker.allocation"].create({
            "task_id": "TSK-DAILY-EP",
            "persona_id": persona.id,
            "tasker_member_id": member.id,
        })
        # The PL must resolve to an hr.employee, or pl_id is empty, no employee is
        # ever grouped, and the AccessError this test exists to catch never fires.
        cls.pl_employee = cls.env["hr.employee"].sudo().search(
            [("user_id", "=", cls.pl_user.id)], limit=1)

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
        body = self._call("/kensei/tracker/daily/data")
        self.assertNotIn(
            "error", body,
            "Daily Tracker data failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])
        self.assertIn("rows", body.get("result", {}))

    def test_daily_filters_load_for_a_pl(self):
        self.authenticate("daily.pl@ethara.ai", "daily.pl.pw")
        body = self._call("/kensei/tracker/daily/filters")
        self.assertNotIn(
            "error", body,
            "Daily Tracker filters failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])
        self.assertIn("pls", body.get("result", {}))

    def test_dashboard_loads_for_a_pl(self):
        """Same trap: the Dashboard's progress table groups by pl_id."""
        self.authenticate("daily.pl@ethara.ai", "daily.pl.pw")
        body = self._call("/kensei/tracker/dashboard")
        self.assertNotIn(
            "error", body,
            "Dashboard failed for a PL: %s"
            % json.dumps(body.get("error", {}))[:400])


class TestTrackerReset(KenseiTrackerCommon):
    """action_reset_task — throw the work away and start the whole task over.

    Distinct from action_reopen, which un-freezes ONE stage and KEEPS its work.
    """

    def _task_with_two_stages(self):
        """A fully-worked stage 1, handed off to a live stage 2."""
        alloc = self._make_alloc(suffix="reset")
        self._complete_stage1(alloc)
        self.env["kensei.tracker.stage.handoff"].with_user(self.user_ql).create({
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
        Alloc = self.env["kensei.tracker.allocation"]
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


class TestTrackerBulkAllocation(KenseiTrackerCommon):

    def test_round_robin_respects_per_tasker_cap(self):
        Wiz = self.env["kensei.tracker.bulk.allocation"]
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
        domain = self.env["kensei.tracker.bulk.allocation"]._unassigned_persona_domain()
        self.assertEqual(domain, [("assignment_status", "=", "unassigned")])

        fresh = self.Persona.create({"name": "bulk-unassigned-probe"})
        self.assertEqual(fresh.assignment_status, "unassigned")
        self._make_alloc(suffix="probe", persona_id=fresh.id)
        fresh.invalidate_recordset()
        self.assertEqual(fresh.assignment_status, "assigned")
