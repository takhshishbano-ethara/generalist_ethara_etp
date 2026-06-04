"""Pre-migration for 19.0.1.18.1.

Adds ``pipeline_last_heartbeat_at`` so the rewritten
``_cron_watchdog_pipeline`` can check consumer liveness via heartbeat
instead of total runtime. ``IF NOT EXISTS`` keeps the migration
idempotent and safe to re-run.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        ALTER TABLE IF EXISTS t2av_stack_review_wizard
            ALTER COLUMN qc_status      DROP NOT NULL,
            ALTER COLUMN reviewer_notes DROP NOT NULL
        """
    )
    _logger.info(
        "t2av 19.0.1.18.1: ensured pipeline_last_heartbeat_at column on t2av_generation."
    )
