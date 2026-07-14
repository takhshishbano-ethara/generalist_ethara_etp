# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.47.0. Backfills submitted_at for already-submitted
evaluators so existing completed assessments show a sensible submission date.

Best-effort: uses write_date as the stamp, guarded by column existence so a
partially-migrated schema never aborts the upgrade."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'etp_assessment_pro_evaluator'
          AND column_name = 'submitted_at'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE etp_assessment_pro_evaluator
        SET submitted_at = write_date
        WHERE state = 'submitted' AND submitted_at IS NULL
    """)
    _logger.info(
        "post-migrate 1.47.0: backfilled submitted_at for %s submitted "
        "evaluator(s)", cr.rowcount)
