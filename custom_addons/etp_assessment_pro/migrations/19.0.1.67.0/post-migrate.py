# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.67.0 (Phase 3 image_ab flaw-injection).

Adds ``flaw_plan_json`` to both the draft (etp.assessment.pro.prompt.question)
and bank (etp.assessment.pro.question) models. Both are plain nullable Text
columns that Odoo's ORM auto-creates during the ``-u`` schema sync, so there is
NOTHING to do here: existing image_ab questions intentionally keep
flaw_plan_json NULL (no backfill — flaw plans are only generated for NEW
questions, and every drift guard no-ops on an empty plan). This script exists so
the version bump carries an explicit, auditable record that the change is
purely additive and non-destructive.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "post-migrate 19.0.1.67.0: flaw_plan_json columns are auto-created and "
        "left NULL on existing questions (no backfill; guards no-op on NULL).")
