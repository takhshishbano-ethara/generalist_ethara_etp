import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
        SELECT 'skoll.disable_auto_hint', 'False', 1, 1, NOW(), NOW()
        WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter WHERE key = 'skoll.disable_auto_hint'
        )
        """
    )
    _logger.info("skoll 19.0.7.0.0: seeded skoll.disable_auto_hint (default False)")
