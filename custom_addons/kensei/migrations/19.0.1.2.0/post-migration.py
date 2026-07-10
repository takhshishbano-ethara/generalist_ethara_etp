# -*- coding: utf-8 -*-
"""Drop orphan columns left by the Kensei Tracker cleanup.

These fields were removed from kensei.tracker.allocation when the workflow was
consolidated into the stage-driven form + Daily Tracker. Deleting a field in
code does not drop its Postgres column, so do it here.
"""
import logging

_logger = logging.getLogger(__name__)

_DROP_COLUMNS = [
    "perfect_score",
    "tasker_qc",
    "pl_qc",
    "baseline_trajectory",
    "qc_result",
    "qc_date",
    "completion_count",
    "date_authoring_start",
    "date_tasker_qc",
    "date_pl_verified",
    "date_ready_baseline",
    "date_baseline_generated",
    "date_pass1",
    "date_manual_qc",
]


def migrate(cr, version):
    for col in _DROP_COLUMNS:
        cr.execute(
            "ALTER TABLE kensei_tracker_allocation DROP COLUMN IF EXISTS %s CASCADE"
            % col
        )
    _logger.info("kensei tracker: dropped %s orphan columns", len(_DROP_COLUMNS))
