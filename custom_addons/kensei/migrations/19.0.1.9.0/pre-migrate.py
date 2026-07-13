# -*- coding: utf-8 -*-
"""Retire the obsolete Kensei "Viewer" group cleanly.

The Viewer group (and its record rule) were dropped from the module's security
data. On upgrade Odoo therefore tries to DELETE the res.groups row — and on a
database where that group is still wired up, the delete is refused:

    The operation cannot be completed: Another model is using the record you are
    trying to delete.
    Thanks to the following constraint: rule_group_rel_group_id_fkey

i.e. an ir.rule still points at the group through the rule_group_rel m2m.
(Odoo removes obsolete records in `id DESC` order, so whether the rule happens to
go before the group is incidental — and on a database whose rule_group_rel FK is
not ON DELETE CASCADE, losing that race hard-fails the whole upgrade.)

So we tear the group down ourselves, in the right order, BEFORE Odoo's data
uninstall runs. Then Odoo finds nothing left to delete and the upgrade proceeds.

SAFETY — the important part:
A record rule with NO groups is a GLOBAL rule in Odoo: it applies to every user.
So simply deleting the rule_group_rel rows would take any rule that referenced
only the Viewer group and silently promote it to apply to EVERYONE. That is why
rules left group-less are deleted outright, and only rules that still have other
groups are merely unlinked from this one.

Likewise ir.model.access rows are DELETED rather than having group_id set to
NULL: a NULL group on an ACL means "everybody", so nulling it would hand the
whole model to all users.
"""

VIEWER_XMLID = ("kensei", "group_kensei_viewer")


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return cr.fetchone()[0] is not None


def migrate(cr, version):
    module, name = VIEWER_XMLID
    cr.execute(
        """SELECT res_id FROM ir_model_data
            WHERE module = %s AND model = 'res.groups' AND name = %s""",
        (module, name),
    )
    row = cr.fetchone()
    if not row:
        return  # already gone (fresh install, or a database that never had it)
    gid = row[0]

    # ---- 1. Record rules -------------------------------------------------
    # A rule left with zero groups becomes GLOBAL (applies to all users), so a
    # rule that only ever belonged to the Viewer must be deleted, not orphaned.
    cr.execute("SELECT rule_group_id FROM rule_group_rel WHERE group_id = %s", (gid,))
    rule_ids = [r[0] for r in cr.fetchall()]

    cr.execute("DELETE FROM rule_group_rel WHERE group_id = %s", (gid,))

    if rule_ids:
        cr.execute(
            """SELECT id FROM ir_rule
                WHERE id = ANY(%s)
                  AND id NOT IN (SELECT rule_group_id FROM rule_group_rel)""",
            (rule_ids,),
        )
        orphaned = [r[0] for r in cr.fetchall()]
        if orphaned:
            cr.execute(
                "DELETE FROM ir_model_data WHERE model = 'ir.rule' AND res_id = ANY(%s)",
                (orphaned,),
            )
            cr.execute("DELETE FROM ir_rule WHERE id = ANY(%s)", (orphaned,))

    # ---- 2. ACLs ---------------------------------------------------------
    # DELETE, never "SET NULL": a NULL group on ir.model.access means EVERY user.
    cr.execute("DELETE FROM ir_model_access WHERE group_id = %s", (gid,))

    # ---- 3. Every remaining m2m that points at the group ------------------
    for table, column in (
        ("res_groups_users_rel", "gid"),          # members
        ("ir_ui_menu_group_rel", "gid"),          # menu visibility
        ("ir_ui_view_group_rel", "group_id"),     # view-level groups
        ("ir_act_window_group_rel", "gid"),       # window actions
        ("ir_act_server_group_rel", "gid"),       # server actions
        ("ir_model_fields_group_rel", "group_id"),
        ("res_groups_report_rel", "gid"),
    ):
        if _table_exists(cr, table):
            cr.execute(f'DELETE FROM "{table}" WHERE "{column}" = %s', (gid,))

    # implied_ids is self-referential: clear BOTH sides, or the privilege ladder
    # (Tasker < QL < PL) keeps a dangling link to the dead group.
    if _table_exists(cr, "res_groups_implied_rel"):
        cr.execute(
            "DELETE FROM res_groups_implied_rel WHERE gid = %s OR hid = %s", (gid, gid)
        )

    # ---- 4. The group itself --------------------------------------------
    cr.execute(
        "DELETE FROM ir_model_data WHERE model = 'res.groups' AND res_id = %s", (gid,)
    )
    cr.execute("DELETE FROM res_groups WHERE id = %s", (gid,))
