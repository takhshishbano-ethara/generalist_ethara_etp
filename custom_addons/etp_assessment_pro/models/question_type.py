# -*- coding: utf-8 -*-
"""The question-type vocabulary — a fixed 7-row reference model.

Generators pick a subset of these via a Many2many (allowed_question_type_ids) to
restrict what a generation run may author. The rows ARE constants.QUESTION_TYPE_
SELECTION, seeded in init() so the taxonomy stays a single code-level constant and
no second vocabulary can drift away from it. There is no menu/action for this
model on purpose — it is reference data, not user-editable.
"""

import logging

from odoo import models, fields

from ..constants import QUESTION_TYPE_SELECTION

_logger = logging.getLogger(__name__)


class EtpAssessmentQuestionType(models.Model):
    _name = "etp.assessment.pro.question.type"
    _description = "Assessment Question Type (generation vocabulary)"
    _order = "sequence, id"

    code = fields.Char(
        string="Code", required=True, index=True,
        help="Stable taxonomy code — one of constants.QUESTION_TYPE_SELECTION.")
    name = fields.Char(string="Name", required=True, translate=False)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    def init(self):
        """Seed / re-sync the vocabulary from constants.QUESTION_TYPE_SELECTION.

        Runs on every install and -u upgrade (after the table is created), so the
        rows and their labels always mirror the constant. Idempotent: creates a
        missing code, refreshes name/sequence for an existing one. A unique index
        on `code` (like models/assessment.py::init) guards against a duplicate
        vocabulary row — mirror of the closed enum, so uniqueness is structural.
        """
        self._cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS etp_pro_question_type_uniq_code
            ON etp_assessment_pro_question_type (code)
        """)
        Type = self.with_context(active_test=False)
        existing = {t.code: t for t in Type.search([])}
        created = 0
        for seq, (code, label) in enumerate(QUESTION_TYPE_SELECTION, start=10):
            row = existing.get(code)
            if row:
                vals = {}
                if row.name != label:
                    vals["name"] = label
                if row.sequence != seq:
                    vals["sequence"] = seq
                if not row.active:
                    vals["active"] = True
                if vals:
                    row.write(vals)
            else:
                Type.create({"code": code, "name": label, "sequence": seq})
                created += 1
        if created:
            _logger.info(
                "etp.assessment.pro.question.type: seeded %d vocabulary row(s).",
                created)
