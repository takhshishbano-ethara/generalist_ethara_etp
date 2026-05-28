"""Pre-migration for 19.0.1.18.0.

Defensively creates new stored columns on ``t2av_generation`` before the
ORM auto-init loads the model registry. Without this, opening any
existing record after a code-only deploy (no ``-u t2av``) raises
``psycopg2.errors.UndefinedColumn`` for fields the registry knows about
but the database does not. ``ADD COLUMN IF NOT EXISTS`` makes the
migration idempotent for databases already reconciled by auto-init.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


_COLUMNS = (
    ("golden_source",         "VARCHAR"),
    ("pipeline_status",       "VARCHAR"),
    ("pipeline_published_at", "TIMESTAMP"),
    ("pipeline_started_at",   "TIMESTAMP"),
    ("pipeline_finished_at",  "TIMESTAMP"),
    ("pipeline_error_text",   "TEXT"),
    ("pipeline_retry_count",  "INTEGER"),
)


def migrate(cr, version):
    if not version:
        return

    for name, sql_type in _COLUMNS:
        cr.execute(
            f'ALTER TABLE t2av_generation '
            f'ADD COLUMN IF NOT EXISTS "{name}" {sql_type}'
        )

    cr.execute(
        """
        UPDATE t2av_generation
           SET pipeline_retry_count = 0
         WHERE pipeline_retry_count IS NULL
        """
    )
    backfilled_retry = cr.rowcount or 0
    cr.execute(
        """
        UPDATE t2av_generation
           SET pipeline_status = 'not_published'
         WHERE pipeline_status IS NULL
        """
    )
    backfilled_status = cr.rowcount or 0
    _logger.info(
        "t2av 19.0.1.18.0: ensured %d new column(s) on t2av_generation; "
        "backfilled pipeline_retry_count=0 on %d row(s); "
        "backfilled pipeline_status='not_published' on %d row(s).",
        len(_COLUMNS), backfilled_retry, backfilled_status,
    )
