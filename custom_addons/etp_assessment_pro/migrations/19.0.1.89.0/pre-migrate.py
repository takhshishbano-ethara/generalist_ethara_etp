# -*- coding: utf-8 -*-
"""Pre-migrate 19.0.1.89.0: remove the ``subjective_justification`` question type.

``subjective_justification`` and ``subjective_rubric`` were mechanically identical
to the candidate (same prompt + free-text answer); they differed only in whether
the grading rubric was admin-supplied or model-generated. A ``subjective_rubric``
with an EMPTY rubric already reproduces the old justification behaviour (the
grader authors its own rubric), so folding one into the other is behaviour-
preserving.

This runs BEFORE the ORM reloads the (now shrunk) Selection, converting every
stored ``subjective_justification`` value to ``subjective_rubric`` on the three
columns that carry a question type:

  * etp_assessment_pro_question.question_type          (bank questions)
  * etp_assessment_pro_prompt_question.question_type   (draft questions)
  * etp_assessment_pro_prompt.force_question_type      (generator force-type)

Existing rows keep a NULL/empty rubric, so they grade exactly as they did before
(generated rubric). Nothing is deleted; historical assessment results that
reference these questions stay valid.
"""
import logging

_logger = logging.getLogger(__name__)

# (table, column) pairs that may hold the retired selection value.
_TARGETS = [
    ("etp_assessment_pro_question", "question_type"),
    ("etp_assessment_pro_prompt_question", "question_type"),
    ("etp_assessment_pro_prompt", "force_question_type"),
]


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return
    total = 0
    for table, column in _TARGETS:
        if not _column_exists(cr, table, column):
            _logger.info(
                "pre-migrate 19.0.1.89.0: %s.%s absent, skipping", table, column)
            continue
        cr.execute(
            """
            UPDATE {table}
               SET {column} = 'subjective_rubric'
             WHERE {column} = 'subjective_justification'
            """.format(table=table, column=column)
        )
        moved = cr.rowcount or 0
        total += moved
        _logger.info(
            "pre-migrate 19.0.1.89.0: %s.%s converted %d "
            "subjective_justification -> subjective_rubric",
            table, column, moved)
    _logger.info(
        "pre-migrate 19.0.1.89.0: converted %d row(s) total; "
        "subjective_justification retired.", total)
