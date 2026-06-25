import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = 'lynceus.reclaim_hours'"
    )
    row = cr.fetchone()
    if not row:
        return
    current = (row[0] or "").strip()
    if current != "24":
        _logger.info(
            "Lynceus migrate 19.0.6.1.4: lynceus.reclaim_hours=%r is a custom "
            "value; leaving untouched.", current,
        )
        return
    cr.execute(
        "UPDATE ir_config_parameter SET value = '12' "
        "WHERE key = 'lynceus.reclaim_hours'"
    )
    _logger.info(
        "Lynceus migrate 19.0.6.1.4: lynceus.reclaim_hours migrated from '24' to '12'."
    )
