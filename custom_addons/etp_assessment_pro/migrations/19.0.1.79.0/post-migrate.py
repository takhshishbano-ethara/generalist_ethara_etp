# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.79.0 (Gap 2 dense image_label generation).

Adds plain nullable columns that Odoo's ORM auto-creates during the ``-u``
schema sync, so there is NOTHING to do here:

- draft (etp.assessment.pro.prompt.question): behavioural_key_json,
  label_boxes_json, coverage_expected, omitted_element_json, label_application;
- bank image (etp.assessment.pro.question.image): label_application.

Existing image_label drafts/questions intentionally keep them NULL — dense
answer keys are only authored for NEW questions, and every dense scoring path
(behavioural key, coverage gate, application checklist) no-ops on an empty
value, falling back to the legacy single-string ideal_labels + post-approve
detection. This script exists so the version bump carries an explicit, auditable
record that the change is purely additive and non-destructive.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "post-migrate 19.0.1.79.0: dense image_label columns are auto-created "
        "and left NULL on existing rows (no backfill; every dense path no-ops "
        "on an empty value and falls back to the legacy single-box path).")
