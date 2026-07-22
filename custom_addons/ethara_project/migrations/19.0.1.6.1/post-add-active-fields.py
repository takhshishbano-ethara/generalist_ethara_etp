import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE ethara_project
           SET active = TRUE
         WHERE active IS NULL
        """
    )
    _logger.info(
        "ethara_project 19.0.1.6.1: backfilled active=TRUE on %s ethara_project rows",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE ethara_project_phase_daily_task
           SET active = TRUE
         WHERE active IS NULL
        """
    )
    _logger.info(
        "ethara_project 19.0.1.6.1: backfilled active=TRUE on %s daily_task rows",
        cr.rowcount,
    )
