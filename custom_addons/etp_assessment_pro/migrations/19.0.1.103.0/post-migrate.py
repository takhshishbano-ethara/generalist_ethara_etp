# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.103.0 (allow-list storage -> Many2many vocabulary).

The generation allow-list moved from a One2many line model to a Many2many into a
seeded 7-row vocabulary (etp.assessment.pro.question.type). This post-migrate:

  1. Backfills the M2M from the legacy scalar ``force_question_type`` so an
     existing forced generator keeps forcing that type.
  2. Defensively drains the old One2many line table
     (``etp_assessment_pro_prompt_gen_type``) into the M2M and drops it. That
     table only ever existed where the never-shipped 19.0.1.102.0 ran (i.e. the
     local dev DB); the table-exists guard makes this a no-op everywhere else.

Runs POST-migrate: the vocabulary is seeded by the model's init() (schema phase)
before this runs, and the legacy ``force_question_type`` column is still present
because models/prompt.py keeps the field as a legacy shadow. Idempotent: skips a
generator that already has M2M links.

Taxonomy-retirement note for the next maintainer: the question-type code now lives
on etp_assessment_pro_question_type.code (a single reference row per type), not on
a per-prompt line. Retiring a type = archive/remove that one vocabulary row; the
M2M links cascade away. This is NOT one of the per-column carriers targeted by
migrations/19.0.1.89.0/pre-migrate.py:28-32, so that pre-migrate needs no change.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:                       # fresh install: nothing to backfill
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    QType = env["etp.assessment.pro.question.type"].with_context(active_test=False)
    Prompt = env["etp.assessment.pro.prompt"]
    by_code = {t.code: t.id for t in QType.search([])}

    # (2) Drain the old One2many line table into the M2M, then drop it. Done
    # before the force_question_type backfill so a prompt migrated here is
    # correctly seen as "already has links" and skipped below.
    drained = 0
    if _table_exists(cr, "etp_assessment_pro_prompt_gen_type"):
        cr.execute(
            "SELECT prompt_id, question_type FROM etp_assessment_pro_prompt_gen_type")
        links = {}
        for prompt_id, question_type in cr.fetchall():
            qtype_id = by_code.get(question_type)
            if prompt_id and qtype_id:
                links.setdefault(prompt_id, set()).add(qtype_id)
        for prompt_id, qtype_ids in links.items():
            prompt = Prompt.browse(prompt_id).exists()
            if prompt and not prompt.allowed_question_type_ids:
                prompt.allowed_question_type_ids = [(6, 0, sorted(qtype_ids))]
                drained += 1
        cr.execute("DROP TABLE IF EXISTS etp_assessment_pro_prompt_gen_type")

    # (1) Backfill from the legacy scalar for any generator still unmapped.
    seeded = 0
    if _column_exists(cr, "etp_assessment_pro_prompt", "force_question_type"):
        prompts = Prompt.search([
            ("force_question_type", "!=", False),
            ("allowed_question_type_ids", "=", False),
        ])
        for prompt in prompts:
            qtype_id = by_code.get(prompt.force_question_type)
            if qtype_id:
                prompt.allowed_question_type_ids = [(4, qtype_id)]
                seeded += 1

    _logger.info(
        "post-migrate 19.0.1.103.0: drained %d generator(s) from the legacy line "
        "table and seeded %d from force_question_type into the question-type "
        "Many2many.", drained, seeded)
