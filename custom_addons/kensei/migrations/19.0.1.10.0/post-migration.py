# -*- coding: utf-8 -*-
"""Security hardening for kensei.tracker.allocation.

Enforces the three-tier access model in the RECORD RULES, where it belongs:

    Admin    every allocation, including rows with no PL/QL assigned.
    PL / QL  their OWN TEAM only — the tasks they are the assigned PL or QL of.
    Tasker   only the tasks assigned to them.

Until now only the Tasker tier was real. QL and PL both carried [(1,'=',1)] —
org-wide access — while controllers/tracker.py scoped the Dashboard to the
lead's team. So a PL saw a correctly-scoped dashboard and then saw EVERY team's
tasks the moment they opened the list view, exported, or called RPC. The rules
now carry the same domain the controller always used.

Three things need doing that a plain module upgrade will NOT do on its own.

1. The rules live in a ``noupdate="1"`` block, which blocks UPDATES to records
   that already exist. Every domain changed below would therefore be silently
   ignored on an existing database and applied only on a fresh install — so the
   domains are forced here, keyed by xmlid.

   (Rules that are NEW in this version do not need this: noupdate suppresses
   updates, never creation, so they are created normally.)

2. A GROUP-LESS (therefore GLOBAL) record rule that 19.0.1.9.0 failed to clean up
   is deleted. See the inline comment — 1.9.0 keyed the cleanup on the Viewer
   group still existing, so on any DB where the group went first, the rule was
   left behind applying to every user.

3. Any allocation whose ``total_stages`` was driven to 0 — which the old
   truthiness-based ``_guard_privileged_fields`` allowed a tasker to do, making
   ``is_final_stage`` (stage_no >= total_stages) True on stage 1 and letting them
   self-certify a stage as 'deliverable' — is repaired back to the intended 2,
   and the stored status computes are re-derived so any wrongly-granted
   'deliverable' collapses back to 'ready_next_stage'.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_OWN = "[('tasker_user_id', '=', user.id)]"
_TEAM = ("['|', ('assigned_pl_id', '=', user.id), "
         "('assigned_ql_id', '=', user.id)]")
_ALL = "[(1, '=', 1)]"

# xmlid -> intended domain. Must stay in lockstep with
# security/kensei_tracker_security.xml; the migration exists only because
# noupdate="1" would otherwise leave the pre-existing rules on their old domains.
_RULE_DOMAINS = {
    'kensei.kensei_tracker_allocation_rule_tasker': _OWN,
    'kensei.kensei_tracker_allocation_rule_ql': _TEAM,
    'kensei.kensei_tracker_allocation_rule_pl': _TEAM,
    'kensei.kensei_tracker_allocation_rule_admin': _ALL,
    'kensei.kensei_tracker_allocation_rule_system': _ALL,
    'kensei.kensei_tracker_allocation_rule_etp_tasker': _OWN,
    'kensei.kensei_tracker_allocation_rule_etp_ql': _TEAM,
    'kensei.kensei_tracker_allocation_rule_etp_pl': _TEAM,
    'kensei.kensei_tracker_allocation_rule_etp_qr': _TEAM,
    'kensei.kensei_tracker_allocation_rule_etp_cto': _ALL,
    'kensei.kensei_tracker_allocation_rule_etp_hr_admin': _ALL,
    'kensei.kensei_tracker_allocation_rule_etp_it_admin': _ALL,
}


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    # ---- 1. force every rule domain past noupdate="1" ----
    for xmlid, domain in _RULE_DOMAINS.items():
        rule = env.ref(xmlid, raise_if_not_found=False)
        if rule and rule.domain_force != domain:
            _logger.info(
                "kensei: %s — domain %s -> %s", xmlid, rule.domain_force, domain)
            rule.domain_force = domain

    # ---- 2. delete any GROUP-LESS rule left on the allocation model ----
    # 19.0.1.9.0 removed the "Viewer" group and meant to delete the rules that
    # only belonged to it — a rule with zero groups is GLOBAL and applies to every
    # user. But it bails out early (`if not row: return`) when the Viewer GROUP is
    # already gone, and on any database where that happened first the RULE survived
    # with no groups: "Tracker Allocation: Viewer sees all", domain [(1,'=',1)].
    #
    # It is inert today (a global TRUE is AND-ed with the group rules, so it is a
    # no-op) but it is a loaded gun: edit that domain and it silently applies to
    # everyone. No rule on this model should ever be global — all access here is
    # group-scoped — so drop any that are, keyed on the CONDITION rather than on
    # the Viewer group, which is what 1.9.0 got wrong.
    cr.execute("""
        SELECT r.id FROM ir_rule r
        JOIN ir_model m ON m.id = r.model_id
                       AND m.model = 'kensei.tracker.allocation'
        WHERE NOT EXISTS (
            SELECT 1 FROM rule_group_rel x WHERE x.rule_group_id = r.id)
    """)
    global_rules = [r[0] for r in cr.fetchall()]
    if global_rules:
        cr.execute(
            "SELECT id, name FROM ir_rule WHERE id = ANY(%s)", (global_rules,))
        for _rid, rname in cr.fetchall():
            _logger.warning(
                "kensei: deleting GROUP-LESS (global) record rule %r on "
                "kensei.tracker.allocation — a rule with no groups applies to "
                "every user.", rname)
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.rule' AND res_id = ANY(%s)",
            (global_rules,))
        cr.execute("DELETE FROM ir_rule WHERE id = ANY(%s)", (global_rules,))

    # ---- 3. repair any allocation with a self-served total_stages ----
    # Guarded, not blanket: only rows that are actually broken are touched, so a
    # re-run is a no-op.
    cr.execute("""
        UPDATE kensei_tracker_allocation
           SET total_stages = 2
         WHERE total_stages IS NULL
            OR total_stages < 1
    """)
    repaired = cr.rowcount
    if repaired:
        _logger.warning(
            "kensei: repaired total_stages on %s allocation(s) that had been "
            "driven below 1 — their status is being re-derived, so any "
            "wrongly-earned 'deliverable' will revert to 'ready_next_stage'.",
            repaired)

    # is_final_stage / status / final_status / function are STORED computes; Odoo
    # will not re-derive them just because their inputs were repaired by raw SQL.
    Alloc = env['kensei.tracker.allocation']
    recs = Alloc.with_context(active_test=False).search([])
    if not recs:
        return
    recs._compute_is_final_stage()
    recs._compute_status()          # also (re)derives final_status
    recs._compute_function()        # function is keyed off status
    recs.flush_recordset()
