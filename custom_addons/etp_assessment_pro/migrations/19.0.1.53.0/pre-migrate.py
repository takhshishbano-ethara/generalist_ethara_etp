# -*- coding: utf-8 -*-
"""Pre-migrate for 19.0.1.53.0.

Phase 1 of removing "category": add the new additive ``generator_id`` columns
BEFORE the ORM loads the updated models, so neither the post-migrate backfill
nor the ORM's own column reconciliation can crash on a missing column. Purely
additive — nothing is dropped, category_id is untouched.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE etp_assessment_pro_question "
        "ADD COLUMN IF NOT EXISTS generator_id INTEGER")
    cr.execute(
        "ALTER TABLE etp_assessment_pro "
        "ADD COLUMN IF NOT EXISTS generator_id INTEGER")
    _logger.info(
        "pre-migrate 1.53.0: ensured generator_id columns exist on "
        "etp_assessment_pro_question and etp_assessment_pro")
