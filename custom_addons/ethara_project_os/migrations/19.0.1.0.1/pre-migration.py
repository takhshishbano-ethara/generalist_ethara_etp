"""Unfreeze this module's security definitions.

Version 1.0.0 shipped the record rules inside a ``noupdate="1"`` block. Odoo stores that
flag per record in ``ir_model_data``, so those rules were written once and then ignored
every later upgrade — a scoping fix in the source changed nothing on a running database.
That was caught by a negative test: a pod member could still read the Knowledge folder
of a project they had never been allocated to, even after the rule was corrected.

Clearing the flag *before* the data files load lets this same upgrade apply the
corrected domains, and keeps every future security fix landing on its own.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_model_data
           SET noupdate = false
         WHERE module = 'ethara_project_os'
           AND model IN ('ir.rule', 'ir.model.access')
           AND noupdate
    """)
    _logger.info('Project OS: unfroze %s security record(s) for update.', cr.rowcount)
