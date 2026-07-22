import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info(
        "ethara_project 19.0.1.5.5: adding user-input delivery fields "
        "(models_used) and converting submitted_trajectories to plain "
        "input. Backfilling defaults for existing rows.",
    )

    cr.execute(
        """
        UPDATE ethara_project_phase
           SET models_used = ''
         WHERE models_used IS NULL
        """
    )
    cr.execute(
        """
        UPDATE ethara_project_phase
           SET submitted_trajectories = 0
         WHERE submitted_trajectories IS NULL
        """
    )

    _logger.info(
        "ethara_project 19.0.1.5.5: backfill complete. "
        "submitted_trajectories is now a plain user-input Integer "
        "(no longer compute-stored). Existing values are preserved.",
    )
