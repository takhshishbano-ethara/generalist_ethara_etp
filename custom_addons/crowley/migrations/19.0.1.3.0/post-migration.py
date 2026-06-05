import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info("Crowley: post-migration to 19.0.1.3.0 starting")
    cr.execute("""
        UPDATE crowley_attempt
        SET review_state = 'pending'
        WHERE state = 'done'
          AND (review_state IS NULL OR review_state = '')
    """)
    backfilled = cr.rowcount
    _logger.info(
        "Crowley: set review_state='pending' on %d existing done attempt(s)",
        backfilled,
    )
    _logger.info("Crowley: post-migration to 19.0.1.3.0 complete")
