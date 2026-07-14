# -*- coding: utf-8 -*-
"""Pre-migrate for 19.0.1.11.0 — flatten the multi-day subsystem into a single
sitting exam. The etp.assessment.pro.day and .day.session models (and their
tables/relations) are removed; responses now live directly under the evaluator.

Candidate responses are PRESERVED: they already carry assessment_evaluator_id,
so detaching day_session_id and dropping the day tables loses no answer. Every
statement is guarded (IF EXISTS / column probes) so the upgrade is idempotent
and safe on a DB that never held multi_day data (e.g. the test DB).
"""
import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return

    if _column_exists(cr, "etp_assessment_pro_response", "day_session_id"):
        cr.execute(
            "UPDATE etp_assessment_pro_response SET day_session_id = NULL")

    cr.execute("DROP INDEX IF EXISTS etp_pro_day_session_uniq_eval_day")
    # The response unique index still references day_session_id; drop it so the
    # model init() rebuilds it on (assessment_evaluator_id, question_id).
    cr.execute("DROP INDEX IF EXISTS etp_pro_response_uniq_eval_q_sess")

    cr.execute(
        "ALTER TABLE etp_assessment_pro_response "
        "DROP COLUMN IF EXISTS day_session_id")

    cr.execute("DROP TABLE IF EXISTS etp_assessment_pro_day_session CASCADE")
    cr.execute("DROP TABLE IF EXISTS etp_assessment_pro_day CASCADE")
    cr.execute("DROP TABLE IF EXISTS etp_assessment_pro_day_question_rel CASCADE")

    if _column_exists(cr, "etp_assessment_pro", "assessment_mode"):
        cr.execute(
            "UPDATE etp_assessment_pro SET assessment_mode = 'single' "
            "WHERE assessment_mode = 'multi_day'")

    _logger.info("pre-migrate 1.11.0: flattened multi-day plan into single "
                 "sitting; responses preserved")
