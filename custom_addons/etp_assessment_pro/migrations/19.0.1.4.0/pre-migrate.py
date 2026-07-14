# -*- coding: utf-8 -*-
"""Pre-migrate for 19.0.1.4.0 — de-duplicate BEFORE the new UNIQUE indexes/
constraints are created by the model init()/_auto_init, so the upgrade cannot
abort on a prod DB that already holds duplicates from before these guards existed.

Creates were previously unguarded, so a concurrent double-submit or a duplicate
id in a frozen question_order could leave duplicate response rows; a concurrent
'Generate Plan' could create a second evaluator / day session for the same
candidate; skill names were never actually unique (dead _sql_constraints).
All must be resolved before init() builds:
  * UNIQUE etp_pro_evaluator_uniq_assess_appl  (evaluator (assessment_id, applicant_id))
  * UNIQUE etp_pro_day_session_uniq_eval_day   (day.session (evaluator_id, day_id))
  * UNIQUE etp_pro_response_uniq_eval_q_sess    (response, P1-3)
  * UNIQUE(name) on etp.assessment.pro.skill    (models.Constraint, P2-3)

Resolved forward (evaluators -> day sessions -> responses) so every downstream
collision surfaced by a merge is cleaned in the same pass.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        WITH keep AS (
            SELECT assessment_id, applicant_id, MIN(id) AS keep_id
            FROM etp_assessment_pro_evaluator
            GROUP BY assessment_id, applicant_id
            HAVING COUNT(*) > 1
        )
        UPDATE etp_assessment_pro_day_session ds
        SET evaluator_id = k.keep_id
        FROM etp_assessment_pro_evaluator e
        JOIN keep k ON k.assessment_id = e.assessment_id
                   AND k.applicant_id = e.applicant_id
        WHERE ds.evaluator_id = e.id AND e.id <> k.keep_id
    """)
    cr.execute("""
        WITH keep AS (
            SELECT assessment_id, applicant_id, MIN(id) AS keep_id
            FROM etp_assessment_pro_evaluator
            GROUP BY assessment_id, applicant_id
            HAVING COUNT(*) > 1
        )
        UPDATE etp_assessment_pro_response r
        SET assessment_evaluator_id = k.keep_id
        FROM etp_assessment_pro_evaluator e
        JOIN keep k ON k.assessment_id = e.assessment_id
                   AND k.applicant_id = e.applicant_id
        WHERE r.assessment_evaluator_id = e.id AND e.id <> k.keep_id
    """)
    cr.execute("""
        DELETE FROM etp_assessment_pro_evaluator a
        USING etp_assessment_pro_evaluator b
        WHERE a.id > b.id
          AND a.assessment_id = b.assessment_id
          AND a.applicant_id = b.applicant_id
    """)
    if cr.rowcount:
        _logger.info("pre-migrate 1.4.0: merged %s duplicate evaluator(s)",
                     cr.rowcount)

    cr.execute("""
        WITH keep AS (
            SELECT evaluator_id, day_id, MIN(id) AS keep_id
            FROM etp_assessment_pro_day_session
            GROUP BY evaluator_id, day_id
            HAVING COUNT(*) > 1
        )
        UPDATE etp_assessment_pro_response r
        SET day_session_id = k.keep_id
        FROM etp_assessment_pro_day_session ds
        JOIN keep k ON k.evaluator_id = ds.evaluator_id
                   AND k.day_id = ds.day_id
        WHERE r.day_session_id = ds.id AND ds.id <> k.keep_id
    """)
    cr.execute("""
        DELETE FROM etp_assessment_pro_day_session a
        USING etp_assessment_pro_day_session b
        WHERE a.id > b.id
          AND a.evaluator_id = b.evaluator_id
          AND a.day_id = b.day_id
    """)
    if cr.rowcount:
        _logger.info("pre-migrate 1.4.0: merged %s duplicate day session(s)",
                     cr.rowcount)

    cr.execute("""
        DELETE FROM etp_assessment_pro_response_line
        WHERE response_id IN (
            SELECT a.id FROM etp_assessment_pro_response a
            JOIN etp_assessment_pro_response b
              ON a.id > b.id
             AND a.assessment_evaluator_id = b.assessment_evaluator_id
             AND a.question_id = b.question_id
             AND COALESCE(a.day_session_id, 0) = COALESCE(b.day_session_id, 0)
        )
    """)
    cr.execute("""
        DELETE FROM etp_assessment_pro_response a
        USING etp_assessment_pro_response b
        WHERE a.id > b.id
          AND a.assessment_evaluator_id = b.assessment_evaluator_id
          AND a.question_id = b.question_id
          AND COALESCE(a.day_session_id, 0) = COALESCE(b.day_session_id, 0)
    """)
    _logger.info("pre-migrate 1.4.0: removed %s duplicate response row(s) "
                 "before creating the unique index", cr.rowcount)

    cr.execute("""
        UPDATE etp_assessment_pro_skill s
        SET name = s.name || ' #' || s.id
        WHERE EXISTS (
            SELECT 1 FROM etp_assessment_pro_skill t
            WHERE t.name = s.name AND t.id < s.id
        )
    """)
    if cr.rowcount:
        _logger.info("pre-migrate 1.4.0: renamed %s duplicate skill name(s)",
                     cr.rowcount)
