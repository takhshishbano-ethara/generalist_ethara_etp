import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        WITH first_msg AS (
            SELECT DISTINCT ON (res_id) res_id, id
            FROM mail_message
            WHERE model = 'ethara.project'
              AND message_type = 'comment'
            ORDER BY res_id, date ASC, id ASC
        )
        UPDATE ethara_project p
        SET email_thread_root_message_id = fm.id
        FROM first_msg fm
        WHERE p.id = fm.res_id
          AND p.email_thread_root_message_id IS NULL
        """
    )
    _logger.info(
        "ethara_project: backfilled email_thread_root_message_id on %s project(s)",
        cr.rowcount,
    )
