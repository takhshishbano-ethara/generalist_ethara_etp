import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info(
        "ethara_project 19.0.1.5.4: backfilling delivery-snapshot fields "
        "(est_trajectories_per_task, submitted_task_count, "
        "delivered_per_task_cost) to zero on existing rows.",
    )

    cr.execute(
        """
        UPDATE ethara_project_budget
           SET est_trajectories_per_task = 0
         WHERE est_trajectories_per_task IS NULL
        """
    )
    cr.execute(
        """
        UPDATE ethara_project_phase
           SET est_trajectories_per_task = 0
         WHERE est_trajectories_per_task IS NULL
        """
    )
    cr.execute(
        """
        UPDATE ethara_project_phase
           SET submitted_task_count = 0
         WHERE submitted_task_count IS NULL
        """
    )
    cr.execute(
        """
        UPDATE ethara_project_phase
           SET delivered_per_task_cost = 0.0
         WHERE delivered_per_task_cost IS NULL
        """
    )

    _logger.info(
        "ethara_project 19.0.1.5.4: delivery-snapshot backfill complete. "
        "Compute-stored fields (total_trajectories, submitted_trajectories, "
        "submitted_batch_total) will be recomputed on registry load.",
    )
