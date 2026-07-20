# -*- coding: utf-8 -*-
"""Retire the 'immediate' results-release mode; keep only 'Manual Release'.

`etp.assessment.pro.results_release` was a required Selection of
{manual, immediate}. Product decision: results are NEVER auto-revealed - an
admin always releases them explicitly. The 'immediate' option is removed from
the field definition.

Any assessment row still holding the string 'immediate' would become an orphan
value the moment the new selection loads (Odoo renders it blank and a write
would raise). We normalize it to 'manual' in PRE-migrate, before the registry
with the narrowed selection is built.

This is safe and non-destructive to candidate visibility: 'immediate' only ever
governed HOW `evaluator.results_released` got flipped (auto on scoring vs admin
button). Rows already released (results_released=True) stay released; rows not
yet released now simply wait for the admin's Release Results click - which is
exactly the manual contract. No evaluator's results_released value is touched.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE etp_assessment_pro
           SET results_release = 'manual'
         WHERE results_release = 'immediate'
        """
    )
    if cr.rowcount:
        _logger.info(
            "pre-migrate 19.0.1.121.0: normalized %d assessment(s) from "
            "results_release='immediate' to 'manual' (Immediate mode retired).",
            cr.rowcount,
        )
