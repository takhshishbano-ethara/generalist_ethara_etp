# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.53.0.

Phase 1 of removing "category": backfill the additive ``generator_id`` link on
existing bank questions and assessments.

Every step runs inside its OWN savepoint and rolls back on failure. This matters
because Odoo runs all pre-migrates before any post-migrate: when 1.53.0 and
1.54.0 are applied in a single upgrade, 1.54.0's pre-migrate has already dropped
the category table/columns before this backfill runs, so the category-dependent
steps must skip cleanly instead of aborting (poisoning) the whole transaction.
Backfill runs in descending authority:
  A  draft->approved join (authoritative, no category dependency)
  B  source_ref 'gen:<name>' parse (no category dependency)
  C  auto-category-name 'Gen: <name>' heuristic (needs category; skips if gone)
  D  assessment: category match (skips if gone), then plurality of its questions
  E  report remaining orphans (left NULL on purpose)
"""
import logging

_logger = logging.getLogger(__name__)


def _try(cr, label, sql):
    cr.execute("SAVEPOINT etp_bf")
    try:
        cr.execute(sql)
        n = cr.rowcount
    except Exception as exc:  # noqa: BLE001 - guarded, logged, rolled back
        cr.execute("ROLLBACK TO SAVEPOINT etp_bf")
        _logger.warning("post-migrate 1.53.0 %s skipped: %s", label, exc)
        return
    cr.execute("RELEASE SAVEPOINT etp_bf")
    _logger.info("post-migrate 1.53.0 %s: %s row(s)", label, n)


def migrate(cr, version):
    if not version:
        return

    _try(cr, "Step A (draft approval join)", """
        UPDATE etp_assessment_pro_question q
        SET generator_id = dq.prompt_id
        FROM etp_assessment_pro_prompt_question dq
        WHERE dq.approved_question_id = q.id
          AND dq.prompt_id IS NOT NULL
          AND q.generator_id IS NULL
    """)

    _try(cr, "Step B (source_ref parse)", """
        UPDATE etp_assessment_pro_question q
        SET generator_id = sub.pid
        FROM (
            SELECT q2.id AS qid,
                   (ARRAY_AGG(p.id ORDER BY p.id DESC))[1] AS pid
            FROM etp_assessment_pro_question q2
            JOIN etp_assessment_pro_prompt p
              ON q2.source_ref = 'gen:' || p.name
            WHERE q2.generator_id IS NULL
              AND q2.source_ref LIKE 'gen:%'
            GROUP BY q2.id
        ) sub
        WHERE q.id = sub.qid AND q.generator_id IS NULL
    """)

    _try(cr, "Step C (category-name heuristic)", """
        UPDATE etp_assessment_pro_question q
        SET generator_id = sub.pid
        FROM (
            SELECT q2.id AS qid,
                   (ARRAY_AGG(p.id ORDER BY p.id DESC))[1] AS pid
            FROM etp_assessment_pro_question q2
            JOIN etp_assessment_pro_category c
              ON q2.category_id = c.id
            JOIN etp_assessment_pro_prompt p
              ON c.name = 'Gen: ' || p.name
            WHERE q2.generator_id IS NULL
            GROUP BY q2.id
        ) sub
        WHERE q.id = sub.qid AND q.generator_id IS NULL
    """)

    _try(cr, "Step D (category match)", """
        UPDATE etp_assessment_pro a
        SET generator_id = sub.pid
        FROM (
            SELECT a2.id AS aid,
                   (ARRAY_AGG(p.id ORDER BY p.id DESC))[1] AS pid
            FROM etp_assessment_pro a2
            JOIN etp_assessment_pro_prompt p
              ON a2.category_id = p.category_id
            WHERE a2.generator_id IS NULL
              AND a2.category_id IS NOT NULL
              AND p.category_id IS NOT NULL
            GROUP BY a2.id
        ) sub
        WHERE a.id = sub.aid AND a.generator_id IS NULL
    """)

    _try(cr, "Step D (plurality)", """
        UPDATE etp_assessment_pro a
        SET generator_id = sub.pid
        FROM (
            SELECT rel.assessment_id AS aid,
                   MODE() WITHIN GROUP (ORDER BY q.generator_id) AS pid
            FROM etp_assessment_pro_question_rel rel
            JOIN etp_assessment_pro_question q
              ON q.id = rel.question_id
            WHERE q.generator_id IS NOT NULL
            GROUP BY rel.assessment_id
        ) sub
        WHERE a.id = sub.aid AND a.generator_id IS NULL
          AND sub.pid IS NOT NULL
    """)

    cr.execute("SAVEPOINT etp_bf")
    try:
        cr.execute("SELECT COUNT(*) FROM etp_assessment_pro_question "
                   "WHERE generator_id IS NULL")
        q_orphans = cr.fetchone()[0]
        cr.execute("SELECT COUNT(*) FROM etp_assessment_pro "
                   "WHERE generator_id IS NULL")
        a_orphans = cr.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - guarded, logged, rolled back
        cr.execute("ROLLBACK TO SAVEPOINT etp_bf")
        _logger.warning("post-migrate 1.53.0 Step E skipped: %s", exc)
    else:
        cr.execute("RELEASE SAVEPOINT etp_bf")
        _logger.warning(
            "post-migrate 1.53.0 Step E: %s question(s), %s assessment(s) "
            "still have no generator_id (left NULL)", q_orphans, a_orphans)
