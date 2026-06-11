"""iris v1.0 -> v1.1 pre-migration.

Renames the legacy free-text ``iris_candidate.target_role`` column to
``target_role_legacy`` BEFORE the registry loads the v1.1 schema, where
``target_role`` becomes a stored related Char on the new ``role_id``
Many2one. The rename preserves every pre-existing label byte-for-byte for
the end-migration (``end-migrate.py``), which maps the strings onto
``iris.role.profile`` records once the seed data has loaded.

Idempotent: guarded by information_schema checks — the rename happens only
when ``target_role`` still exists, ``role_id`` is absent (the v1.1 schema
has not loaded yet) and ``target_role_legacy`` was not already created by
a previous run.
"""

import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        # Fresh install — no legacy data, nothing to migrate.
        return
    if not _column_exists(cr, "iris_candidate", "target_role"):
        _logger.info(
            "iris pre-migrate 19.0.1.1.0: no target_role column — skipping."
        )
        return
    if _column_exists(cr, "iris_candidate", "role_id"):
        _logger.info(
            "iris pre-migrate 19.0.1.1.0: role_id already exists — "
            "v1.1 schema already in place, skipping rename."
        )
        return
    if _column_exists(cr, "iris_candidate", "target_role_legacy"):
        _logger.info(
            "iris pre-migrate 19.0.1.1.0: target_role_legacy already "
            "exists — rename already done, skipping."
        )
        return
    cr.execute(
        "ALTER TABLE iris_candidate "
        "RENAME COLUMN target_role TO target_role_legacy"
    )
    _logger.info(
        "iris pre-migrate 19.0.1.1.0: renamed iris_candidate.target_role "
        "to target_role_legacy."
    )
