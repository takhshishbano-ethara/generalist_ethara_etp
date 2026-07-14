# -*- coding: utf-8 -*-
"""Drop the Kensei Tracker's tables — the Tracker now lives in `kensei2`.

The Tracker moved to `kensei2` in 19.0.1.12.0: its models were unregistered and its
menus, actions and views removed. What was left behind were the physical tables,
orphaned but still on disk. This drops them.

NO DATA IS CARRIED ACROSS — BY DESIGN
-------------------------------------
kensei2's Tracker starts empty. That is a deliberate product decision, not an
oversight: this is a fresh start, and the old allocations / roster are not wanted.
So there is no copy step and no "is it safe yet?" gate — there is nothing to
preserve, and a gate guarding nothing is just a thing that can go wrong.

If you ever DO need the old rows back, they are in your backup. Take one before you
deploy this; that is the only recovery path, and it is the reason to take it.

IDEMPOTENT
----------
DROP TABLE IF EXISTS, so a table that is already gone is skipped. On a fresh install
there is nothing to drop and this does nothing at all.
"""
import logging

_logger = logging.getLogger(__name__)

# Every table the Tracker owned inside `kensei`. Ordered children-first: the m2m and
# the line table reference their parents, and CASCADE would take them anyway, but
# spelling out the order keeps the intent legible.
TABLES = [
    "kensei_tracker_allocation_kensei_tracker_persona_assign_rel",
    "kensei_tracker_persona_assign",
    "kensei_tracker_bulk_allocation_line",
    "kensei_tracker_bulk_allocation",
    "kensei_tracker_team_import",
    "kensei_tracker_stage_handoff",
    "kensei_persona_import",
    "kensei_tracker_allocation",
    "kensei_tracker_team_member",
]


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return cr.fetchone()[0] is not None


def migrate(cr, version):
    if not version:
        return  # fresh install — no legacy tables exist

    dropped, rows = [], 0
    for table in TABLES:
        if not _table_exists(cr, table):
            continue
        cr.execute('SELECT count(*) FROM "%s"' % table)
        rows += cr.fetchone()[0]
        cr.execute('DROP TABLE IF EXISTS "%s" CASCADE' % table)
        dropped.append(table)

    if not dropped:
        _logger.info("kensei: no legacy Tracker tables to drop.")
        return

    _logger.warning(
        "kensei: dropped %s legacy Tracker table(s) holding %s row(s). The Tracker "
        "lives in kensei2 now and starts empty — this data was NOT carried across, "
        "by design. Tables: %s",
        len(dropped), rows, ", ".join(dropped))
