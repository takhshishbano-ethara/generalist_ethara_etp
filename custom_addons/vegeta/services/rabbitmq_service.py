"""DEPRECATED — RabbitMQ orchestration removed in 19.0.2.0.0.

The Vegeta pipeline now uses **direct async Lambda invocation**
(``boto3 lambda:Invoke(InvocationType='Event')``) for batch fan-out.
Capacity is bounded by the Lambda's ``ReservedConcurrentExecutions``
setting (default: 250) — not by RabbitMQ consumers.

This module is retained only so existing import paths fail loudly with
a clear migration message instead of a silent ``ImportError``.

If you see this stack trace, you have stale code somewhere referencing
``rabbitmq_service``. Replace any call site with the new entry point:

    # OLD
    from .services.rabbitmq_service import batch_publish_vegeta_tasks
    batch_publish_vegeta_tasks(record_ids)

    # NEW
    records.action_run_batch_concurrent()  # fires async Lambda invokes in parallel
"""


class VegetaQueueRemovedError(RuntimeError):
    """Raised when deprecated RabbitMQ entry points are called."""


_MIGRATION_MSG = (
    "RabbitMQ orchestration was removed in vegeta 19.0.2.0.0. "
    "Use vegeta.job.action_run_batch_concurrent() instead. "
    "See custom_addons/vegeta/docs/EKS_DEPLOYMENT.md for the new "
    "async-Lambda architecture (250 concurrent without a queue)."
)


def publish_vegeta_task(record_id, action="run_pipeline"):
    raise VegetaQueueRemovedError(_MIGRATION_MSG)


def batch_publish_vegeta_tasks(record_ids, action="run_pipeline"):
    raise VegetaQueueRemovedError(_MIGRATION_MSG)
