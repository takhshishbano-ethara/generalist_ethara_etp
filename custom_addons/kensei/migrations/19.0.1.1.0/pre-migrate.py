"""Remove orphan groups (group_kensei_user, group_kensei_admin) from initial install.

These were defined in the first kensei_security.xml but later replaced by
group_kensei_tasker / group_kensei_ql / group_kensei_pl.
Odoo doesn't auto-delete removed XML records — they linger in DB and confuse
the permissions UI.
"""

import logging

_logger = logging.getLogger(__name__)

ORPHAN_XMLIDS = [
    "kensei.group_kensei_user",
    "kensei.group_kensei_admin",
]


def migrate(cr, version):
    if not version:
        return

    for xmlid in ORPHAN_XMLIDS:
        module, name = xmlid.split(".")

        cr.execute(
            """
            SELECT res_id FROM ir_model_data
            WHERE module = %s AND name = %s AND model = 'res.groups'
            """,
            (module, name),
        )
        row = cr.fetchone()
        if not row:
            _logger.info("Orphan group %s not found in DB, skipping.", xmlid)
            continue

        group_id = row[0]

        cr.execute(
            "DELETE FROM res_groups_users_rel WHERE gid = %s",
            (group_id,),
        )
        removed_users = cr.rowcount

        cr.execute(
            "DELETE FROM res_groups_implied_rel WHERE gid = %s OR hid = %s",
            (group_id, group_id),
        )

        cr.execute("DELETE FROM res_groups WHERE id = %s", (group_id,))

        cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s AND model = 'res.groups'",
            (module, name),
        )

        _logger.info(
            "Deleted orphan group %s (id=%d, removed %d user assignments).",
            xmlid,
            group_id,
            removed_users,
        )
