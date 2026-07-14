"""Decouple per-question dimensions from the (now removed) master library.

Retargets ``etp.assessment.pro.question.dimension`` /
``.question.dimension.option`` to be self-contained (a plain ``name`` column
instead of a FK to the master ``etp.assessment.pro.dimension`` / ``.option``),
retargets the response-line FKs, then drops the master tables.

Every step is guarded and idempotent so it is safe to re-run and safe on a DB
that only has partial (or no) master data.
"""
import logging

_logger = logging.getLogger(__name__)


def _try(cr, label, sql):
    """Run one statement in a SAVEPOINT so a failure (e.g. a table/column that
    is already gone) does not abort the whole migration transaction."""
    cr.execute("SAVEPOINT etp_qdim")
    try:
        cr.execute(sql)
    except Exception as exc:  # noqa: BLE001 - guarded, logged, rolled back
        cr.execute("ROLLBACK TO SAVEPOINT etp_qdim")
        _logger.warning("pre-migrate 19.0.1.12.0 step %s skipped: %s", label, exc)
    else:
        cr.execute("RELEASE SAVEPOINT etp_qdim")


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return cr.fetchone()[0] is not None


def migrate(cr, version):
    qd = "etp_assessment_pro_question_dimension"
    qdo = "etp_assessment_pro_question_dimension_option"
    rl = "etp_assessment_pro_response_line"
    master = "etp_assessment_pro_dimension"

    have_master = _table_exists(cr, master)

    # Step 1: question.dimension gets a self-contained name (copied from the
    # master's name), defaulting to 'Dimension' for anything unresolved.
    _try(cr, "1a", f'ALTER TABLE {qd} ADD COLUMN IF NOT EXISTS name VARCHAR')
    if have_master:
        _try(cr, "1b", f"""
            UPDATE {qd} AS q
               SET name = d.name
              FROM {master} AS d
             WHERE d.id = q.dimension_id
               AND (q.name IS NULL OR q.name = '')
        """)
    _try(cr, "1c",
         f"UPDATE {qd} SET name = 'Dimension' WHERE name IS NULL OR name = ''")

    # Step 2: option name column already exists (was a stored related); ensure
    # no NULL/blank survives now that it becomes a plain required Char.
    _try(cr, "2",
         f"UPDATE {qdo} SET name = 'Option' WHERE name IS NULL OR name = ''")

    # Step 3: response.line gets question_dimension_id, resolved via the master
    # dimension it used to share with the question.
    _try(cr, "3a",
         f'ALTER TABLE {rl} ADD COLUMN IF NOT EXISTS question_dimension_id INTEGER')
    if have_master:
        _try(cr, "3b", f"""
            UPDATE {rl} AS l
               SET question_dimension_id = q.id
              FROM {qd} AS q,
                   etp_assessment_pro_response AS r
             WHERE r.id = l.response_id
               AND q.question_id = r.question_id
               AND q.dimension_id = l.dimension_id
               AND l.question_dimension_id IS NULL
        """)

    # Step 4: response.line selected option retargets to the per-question option
    # (matched through the old master_option_id it mirrored).
    _try(cr, "4a",
         f'ALTER TABLE {rl} ADD COLUMN IF NOT EXISTS selected_qd_option_id INTEGER')
    if have_master:
        _try(cr, "4b", f"""
            UPDATE {rl} AS l
               SET selected_qd_option_id = o.id
              FROM {qdo} AS o,
                   {qd} AS q,
                   etp_assessment_pro_response AS r
             WHERE q.id = o.question_dimension_id
               AND r.id = l.response_id
               AND q.question_id = r.question_id
               AND q.dimension_id = l.dimension_id
               AND o.master_option_id = l.selected_option_id
               AND l.selected_qd_option_id IS NULL
        """)

    # Step 5/6: drop the FKs from the per-question tables into the master.
    _try(cr, "5", f"ALTER TABLE {qd} DROP COLUMN IF EXISTS dimension_id CASCADE")
    _try(cr, "6", f"ALTER TABLE {qdo} DROP COLUMN IF EXISTS master_option_id CASCADE")

    # Step 7: response.line — drop orphans that could not be retargeted (would
    # violate the required question_dimension_id), then swap the old FK columns
    # out and rename the new selected option column into place.
    cr.execute("SAVEPOINT etp_qdim")
    try:
        cr.execute(f"SELECT COUNT(*) FROM {rl} WHERE question_dimension_id IS NULL")
        orphans = cr.fetchone()[0]
        if orphans:
            _logger.warning(
                "pre-migrate 19.0.1.12.0: deleting %s response line(s) that "
                "could not be retargeted to a question dimension.", orphans)
        cr.execute(f"DELETE FROM {rl} WHERE question_dimension_id IS NULL")
    except Exception as exc:  # noqa: BLE001
        cr.execute("ROLLBACK TO SAVEPOINT etp_qdim")
        _logger.warning("pre-migrate 19.0.1.12.0 step 7-delete skipped: %s", exc)
    else:
        cr.execute("RELEASE SAVEPOINT etp_qdim")

    _try(cr, "7b", f"ALTER TABLE {rl} DROP COLUMN IF EXISTS dimension_id CASCADE")
    _try(cr, "7c",
         f"ALTER TABLE {rl} DROP COLUMN IF EXISTS selected_option_id CASCADE")
    _try(cr, "7d", f"""
        ALTER TABLE {rl}
        RENAME COLUMN selected_qd_option_id TO selected_option_id
    """)

    # Step 8: drop the master tables (options first for the FK).
    _try(cr, "8a",
         "DROP TABLE IF EXISTS etp_assessment_pro_dimension_option CASCADE")
    _try(cr, "8b", "DROP TABLE IF EXISTS etp_assessment_pro_dimension CASCADE")

    # Step 9: purge the master models' XML-id / model bookkeeping.
    _try(cr, "9", """
        DELETE FROM ir_model_data
         WHERE module = 'etp_assessment_pro'
           AND model IN ('etp.assessment.pro.dimension',
                         'etp.assessment.pro.dimension.option')
    """)
