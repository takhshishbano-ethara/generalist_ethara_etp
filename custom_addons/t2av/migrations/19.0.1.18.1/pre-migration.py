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
        'ALTER TABLE t2av_generation '
        'ADD COLUMN IF NOT EXISTS pipeline_last_heartbeat_at TIMESTAMP'
    )
    _logger.info(
        "t2av 19.0.1.18.1: ensured pipeline_last_heartbeat_at column on t2av_generation."
    )
