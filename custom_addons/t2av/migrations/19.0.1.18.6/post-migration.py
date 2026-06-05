from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def _xmlid_to_resid(cr, module, name):
    cr.execute(
        "SELECT res_id FROM ir_model_data WHERE module=%s AND name=%s",
        (module, name),
    )
    row = cr.fetchone()
    return row[0] if row else None


def migrate(cr, version):
    if not version:
        return
    consumer_gid = _xmlid_to_resid(cr, "t2av", "group_t2av_consumer")
    manager_gid = _xmlid_to_resid(cr, "t2av", "group_t2av_manager")
    if not consumer_gid or not manager_gid:
        _logger.warning(
            "t2av 19.0.1.18.6: missing xmlid(s) consumer=%s manager=%s; "
            "skipping implied_ids repair.",
            consumer_gid, manager_gid,
        )
        return
    # security/t2av_consumer_security.xml uses <data noupdate="1">, so the
    # implied_ids edge declared in XML is never reapplied to pre-existing DBs.
    cr.execute(
        """
        INSERT INTO res_groups_implied_rel (gid, hid)
        VALUES (%s, %s)
        ON CONFLICT (gid, hid) DO NOTHING
        """,
        (consumer_gid, manager_gid),
    )
    edge_inserted = cr.rowcount
    cr.execute(
        """
        INSERT INTO res_groups_users_rel (gid, uid)
        SELECT %s, uid
          FROM res_groups_users_rel
         WHERE gid = %s
        ON CONFLICT (gid, uid) DO NOTHING
        """,
        (manager_gid, consumer_gid),
    )
    users_propagated = cr.rowcount
    _logger.info(
        "t2av 19.0.1.18.6: reconciled group_t2av_consumer.implied_ids "
        "(edge_inserted=%d, users_propagated_to_manager=%d).",
        edge_inserted, users_propagated,
    )
