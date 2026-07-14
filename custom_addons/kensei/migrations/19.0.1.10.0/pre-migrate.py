# -*- coding: utf-8 -*-
"""Tear down obsolete Kensei security groups BEFORE Odoo's data uninstall.

THE FAILURE THIS FIXES
----------------------
Upgrading kensei on a database that still carries the retired "Viewer" group
dies with:

    Validation Error
    The operation cannot be completed: Another model is using the record you are
    trying to delete.
    Thanks to the following constraint: rule_group_rel_group_id_fkey

The group was dropped from the module's security XML, so on upgrade Odoo tries to
DELETE the res.groups row — while an ir.rule still points at it through the
rule_group_rel m2m.

WHY 19.0.1.9.0's SCRIPT DID NOT COVER IT
----------------------------------------
19.0.1.9.0/pre-migrate.py does exactly this teardown, but it is a migration *to*
1.9.0: it only runs on a database whose installed version is BELOW 19.0.1.9.0. A
database already stamped 19.0.1.9.0 — because the code was deployed without the
migration ever executing — skips it forever, and every later upgrade re-hits the
same foreign key.

Two things then decide whether you actually see the error, which is why it strikes
some databases and not others:

  * Odoo removes obsolete records in id DESC order, so whether the RULE happens to
    be deleted before the GROUP is pure luck of the id sequence; and
  * on a database whose rule_group_rel FK is ON DELETE CASCADE the delete cleans
    up after itself, while on an older schema where it is not, it hard-fails.

Reproduced locally against a non-cascade FK: the upgrade exits 255 with
ForeignKeyViolation on rule_group_rel_group_id_fkey. With this script it passes.

WHAT IT DOES
------------
Deletes every res.groups owned by THIS module whose xmlid is no longer one the
module defines — i.e. any stale group, not just the Viewer — after unwiring every
relation that points at it. Idempotent: a database with nothing stale does no work.

SAFETY — the part that matters
------------------------------
A record rule with NO groups is a GLOBAL rule in Odoo: it applies to EVERY user.
So simply deleting the rule_group_rel rows would silently promote any rule that
belonged only to the dead group into a rule that applies to everybody. Rules left
group-less are therefore DELETED outright; rules that still carry other groups are
merely unlinked from this one.

Likewise ir.model.access rows are DELETED rather than having group_id set to NULL:
a NULL group on an ACL means "everybody", so nulling it would hand the whole model
to all users.
"""
import logging

_logger = logging.getLogger(__name__)

# The groups this module still defines (security/kensei_security.xml). Anything
# else that ir_model_data attributes to 'kensei' as a res.groups is obsolete.
CURRENT_GROUP_XMLIDS = {
    "group_kensei_tasker",
    "group_kensei_ql",
    "group_kensei_pl",
    "group_kensei_admin",
}

# Every m2m that can pin a res.groups in place.
GROUP_RELATIONS = (
    ("res_groups_users_rel", "gid"),          # members
    ("ir_ui_menu_group_rel", "gid"),          # menu visibility
    ("ir_ui_view_group_rel", "group_id"),     # view-level groups
    ("ir_act_window_group_rel", "gid"),       # window actions
    ("ir_act_server_group_rel", "gid"),       # server actions
    ("ir_model_fields_group_rel", "group_id"),
    ("res_groups_report_rel", "gid"),
)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return cr.fetchone()[0] is not None


def _teardown_group(cr, gid, xmlid):
    # ---- 1. record rules -------------------------------------------------
    cr.execute("SELECT rule_group_id FROM rule_group_rel WHERE group_id = %s", (gid,))
    rule_ids = [r[0] for r in cr.fetchall()]
    cr.execute("DELETE FROM rule_group_rel WHERE group_id = %s", (gid,))

    if rule_ids:
        # Anything left with zero groups is now GLOBAL — delete, never orphan.
        cr.execute(
            """SELECT id FROM ir_rule
                WHERE id = ANY(%s)
                  AND id NOT IN (SELECT rule_group_id FROM rule_group_rel)""",
            (rule_ids,),
        )
        orphaned = [r[0] for r in cr.fetchall()]
        if orphaned:
            _logger.warning(
                "kensei: deleting %s record rule(s) left group-less by removing "
                "%s — a rule with no groups applies to EVERY user.",
                len(orphaned), xmlid)
            cr.execute(
                "DELETE FROM ir_model_data WHERE model = 'ir.rule' AND res_id = ANY(%s)",
                (orphaned,))
            cr.execute("DELETE FROM ir_rule WHERE id = ANY(%s)", (orphaned,))

    # ---- 2. ACLs — DELETE, never "SET NULL" (NULL group = everybody) ------
    cr.execute("DELETE FROM ir_model_access WHERE group_id = %s", (gid,))

    # ---- 3. every other m2m that points at the group ----------------------
    for table, column in GROUP_RELATIONS:
        if _table_exists(cr, table):
            cr.execute(f'DELETE FROM "{table}" WHERE "{column}" = %s', (gid,))

    # implied_ids is self-referential: clear BOTH sides, or the privilege ladder
    # keeps a dangling link to the dead group.
    if _table_exists(cr, "res_groups_implied_rel"):
        cr.execute(
            "DELETE FROM res_groups_implied_rel WHERE gid = %s OR hid = %s",
            (gid, gid))

    # ---- 4. the group itself ---------------------------------------------
    cr.execute(
        "DELETE FROM ir_model_data WHERE model = 'res.groups' AND res_id = %s",
        (gid,))
    cr.execute("DELETE FROM res_groups WHERE id = %s", (gid,))


def migrate(cr, version):
    if not version:
        return  # fresh install: nothing stale can exist

    cr.execute(
        """SELECT name, res_id FROM ir_model_data
            WHERE module = 'kensei' AND model = 'res.groups'""")
    stale = [(name, res_id) for name, res_id in cr.fetchall()
             if name not in CURRENT_GROUP_XMLIDS]

    if not stale:
        return

    for name, gid in stale:
        _logger.warning(
            "kensei: tearing down obsolete security group 'kensei.%s' (id=%s) "
            "before Odoo's data uninstall — leaving it wired up is what raises "
            "'Another model is using the record you are trying to delete' on "
            "rule_group_rel_group_id_fkey.", name, gid)
        _teardown_group(cr, gid, "kensei.%s" % name)
