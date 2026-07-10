# -*- coding: utf-8 -*-
"""Drop the removed Kensei Tracker persona-roster table.

The separate ``kensei.tracker.persona`` roster was removed in favour of reusing
the existing ``kensei.persona`` master (single source of truth). Deleting a
model in code does not drop its Postgres table, so do it here.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("DROP TABLE IF EXISTS kensei_tracker_persona CASCADE")
    _logger.info("kensei tracker: dropped table kensei_tracker_persona")
