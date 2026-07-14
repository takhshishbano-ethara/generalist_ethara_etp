# -*- coding: utf-8 -*-
"""Pre-migrate for 19.0.1.54.0.

Phase 2 of removing "category": ``generator_id`` now drives question selection
everywhere, so the ``etp.assessment.pro.category`` model, its columns and its
join table are dropped. Runs BEFORE the ORM loads the updated models so the
registry never reconciles against the stale ``category_id`` FKs.

Every step is guarded in its own SAVEPOINT and idempotent — safe to re-run and
safe on a DB where category was already partially removed.
"""
import logging

_logger = logging.getLogger(__name__)


def _try(cr, label, sql):
    cr.execute("SAVEPOINT etp_cat")
    try:
        cr.execute(sql)
    except Exception as exc:  # noqa: BLE001 - guarded, logged, rolled back
        cr.execute("ROLLBACK TO SAVEPOINT etp_cat")
        _logger.warning("pre-migrate 19.0.1.54.0 step %s skipped: %s", label, exc)
    else:
        cr.execute("RELEASE SAVEPOINT etp_cat")


def migrate(cr, version):
    if not version:
        return

    _try(cr, "1", "ALTER TABLE etp_assessment_pro_question "
                  "DROP COLUMN IF EXISTS category_id CASCADE")
    _try(cr, "2", "ALTER TABLE etp_assessment_pro "
                  "DROP COLUMN IF EXISTS category_id CASCADE")
    _try(cr, "3", "ALTER TABLE etp_assessment_pro_prompt "
                  "DROP COLUMN IF EXISTS category_id CASCADE")
    _try(cr, "4", "ALTER TABLE etp_assessment_pro_prompt_question "
                  "DROP COLUMN IF EXISTS category_id CASCADE")

    _try(cr, "5", "DROP TABLE IF EXISTS etp_assessment_pro_category_addq_rel")
    _try(cr, "6", "DROP TABLE IF EXISTS etp_assessment_pro_category CASCADE")

    _try(cr, "7", """
        DELETE FROM ir_model_data
         WHERE module = 'etp_assessment_pro'
           AND model = 'etp.assessment.pro.category'
    """)

    _logger.info("pre-migrate 19.0.1.54.0: dropped category columns and table; "
                 "generator_id now drives question selection. The model, views, "
                 "menu and ACL bookkeeping are left for Odoo's orphan cleanup.")
