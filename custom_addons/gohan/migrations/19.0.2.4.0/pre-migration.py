"""Spec-alignment migration (HANDOFF_ODOO.md §4.3).

Renames ``gohan.category.technical_key`` to ``code`` and re-aliases the seeded
category xmlids from ``category_*`` to ``cat_*``.  Idempotent: re-running on
an already-migrated database is a no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'gohan_category'
           AND column_name = 'technical_key'
        """
    )
    if cr.fetchone():
        _logger.info("[gohan] renaming gohan_category.technical_key -> code")
        cr.execute(
            "ALTER TABLE gohan_category RENAME COLUMN technical_key TO code"
        )
    else:
        _logger.info("[gohan] gohan_category.code already present; skipping rename")

    # Step 1: drop stale "category_*" rows whose "cat_*" twin already exists.
    # This handles the broken-middle-state case (both versions coexist) so the
    # subsequent UPDATE below cannot hit the (module, name) unique constraint.
    cr.execute(
        """
        DELETE FROM ir_model_data old
              USING ir_model_data twin
              WHERE old.module = 'gohan'
                AND old.model  = 'gohan.category'
                AND old.name LIKE 'category_%'
                AND twin.module = 'gohan'
                AND twin.model  = 'gohan.category'
                AND twin.name   = 'cat_' || substring(old.name FROM 10)
        """
    )
    _logger.info(
        "[gohan] removed %s stale category_* duplicates (cat_* twin already present)",
        cr.rowcount,
    )

    # Step 2: safely rename any remaining category_* xmlids (no conflicts left).
    cr.execute(
        """
        UPDATE ir_model_data
           SET name = 'cat_' || substring(name FROM 10)
         WHERE module = 'gohan'
           AND name LIKE 'category_%'
           AND model = 'gohan.category'
        """
    )
    _logger.info(
        "[gohan] re-aliased %s category xmlids: category_* -> cat_*",
        cr.rowcount,
    )
