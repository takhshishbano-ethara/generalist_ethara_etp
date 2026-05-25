# -*- coding: utf-8 -*-
"""Post-migration: deactivate removed cron jobs.

The build/run polling crons were defined with ``noupdate="1"`` so they
survive XML deletions.  Explicitly set ``active = False`` to ensure
they stop firing in all environments.
"""

import logging

_logger = logging.getLogger(__name__)

CRON_XMLIDS = [
    "kaiju_commit0.ir_cron_poll_build_status",
    "kaiju_commit0.ir_cron_poll_run_status",
]


def migrate(cr, version):
    for xmlid in CRON_XMLIDS:
        module, name = xmlid.split(".", 1)
        cr.execute(
            """
            UPDATE ir_cron SET active = false
            WHERE id = (
                SELECT res_id FROM ir_model_data
                WHERE module = %s AND name = %s AND model = 'ir.cron'
            )
            AND active = true
            """,
            (module, name),
        )
        if cr.rowcount:
            _logger.info("Deactivated cron %s", xmlid)
        else:
            _logger.info("Cron %s already inactive or not found", xmlid)
