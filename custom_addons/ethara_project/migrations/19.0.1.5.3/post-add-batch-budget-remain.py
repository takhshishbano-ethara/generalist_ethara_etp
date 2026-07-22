import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE ethara_project_budget
        SET batch_budget_remain = 0.0
        WHERE batch_budget_remain IS NULL
        """
    )
    _logger.info(
        "ethara_project: initialised batch_budget_remain=0.0 on %s budget(s)",
        cr.rowcount,
    )
