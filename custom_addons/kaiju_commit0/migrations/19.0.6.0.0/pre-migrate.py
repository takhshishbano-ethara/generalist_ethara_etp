# -*- coding: utf-8 -*-
"""Pre-migration: rename display_name → step_name on workflow step table.

Odoo's base model reserves ``display_name`` as a computed field.  Our
stored override conflicted, causing writes to be silently dropped.
Rename the column so the ORM picks it up under the new field name and
existing data is preserved.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'kaiju_commit0_workflow_step'
          AND column_name = 'display_name'
    """)
    if cr.fetchone():
        cr.execute("""
            ALTER TABLE kaiju_commit0_workflow_step
            RENAME COLUMN display_name TO step_name
        """)
        _logger.info(
            "Renamed column display_name → step_name on kaiju_commit0_workflow_step"
        )
    else:
        _logger.info(
            "Column display_name not found on kaiju_commit0_workflow_step — "
            "nothing to rename (fresh install or already migrated)"
        )
