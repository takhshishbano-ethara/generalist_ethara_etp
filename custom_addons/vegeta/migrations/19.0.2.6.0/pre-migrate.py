"""Pre-migration for 19.0.2.6.0: add current_phase column.

The ``current_phase`` field was added to vegeta.job to surface sub-step
progress within a state (e.g. "generating.calling_bedrock").  Existing
databases lack this column and raise UndefinedColumn on any read.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        ALTER TABLE vegeta_job
          ADD COLUMN IF NOT EXISTS current_phase VARCHAR NOT NULL DEFAULT ''
        """
    )
    _logger.info("[vegeta] 19.0.2.6.0: added current_phase column to vegeta_job")
