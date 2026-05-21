"""Aurora 19.0.4.0.1 pre-migration: remove notify-admin workflow.

Moves any aurora.harness.staging records currently at stage='notified' to
stage='done' so they are actionable from the new UI (the 'Notify Admin'
button and its target state have been removed).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'aurora_harness_staging'"
    )
    if not cr.fetchone():
        return
    cr.execute(
        "UPDATE aurora_harness_staging SET stage = 'done' WHERE stage = 'notified'"
    )
    moved = cr.rowcount
    if moved:
        _logger.info(
            "Aurora 19.0.4.0.1: moved %d harness staging record(s) from 'notified' to 'done'",
            moved,
        )
