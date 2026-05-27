import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        UPDATE t2av_generation
           SET state = 'draft'
         WHERE state = 'not_assigned'
        """
    )
    moved = cr.rowcount
    if moved:
        _logger.info(
            "T2AV 19.0.1.17.0: migrated %s rows from state='not_assigned' to 'draft'.",
            moved,
        )
