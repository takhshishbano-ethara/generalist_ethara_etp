import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value, create_date, write_date)
        VALUES ('t2av.ambiguity_recovery_enabled', 'False', NOW(), NOW())
        ON CONFLICT (key) DO NOTHING
        """
    )

    cr.execute(
        """
        UPDATE t2av_generation
        SET raw_prompt = prompt
        WHERE raw_prompt IS NULL OR raw_prompt = ''
        """
    )
    backfilled = cr.rowcount

    _logger.info(
        "t2av 19.0.1.19.1: ambiguity recovery feature flag set to OFF "
        "(admin opt-in after staging verification); backfilled raw_prompt "
        "on %d existing generation records.",
        backfilled,
    )
