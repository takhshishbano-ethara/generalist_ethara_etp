"""Post-migration for the long-lived worker-pool PRD architecture (19.0.2.5.0).

After the rolling upgrade from 19.0.2.4.0, two classes of in-flight job
records will be left in a stuck state that workers cannot recover on
their own:

1. K8s-Job-dispatched jobs from the previous version
   (``job_name LIKE 'vegeta-prd-%'``). Their K8s Job either succeeded
   silently or was deleted by ``ttlSecondsAfterFinished`` while the old
   reconcile cron was being replaced. The new reconcile cron only acts
   on stale heartbeats and would leave them parked forever.

2. In-process jobs with the ``inprocess`` sentinel whose Odoo worker
   thread was killed by the rolling deploy mid-run. The reconcile cron
   WILL pick these up via the stale-heartbeat path, but only after the
   300 s threshold — clearing them here makes the new worker pool start
   draining the queue immediately.

For both classes, clearing ``job_name`` hands the job back to the queue
so the new worker daemons can claim it on their next ``_claim_jobs``
tick (a few seconds after the new Deployment rolls out).
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE vegeta_job
           SET job_name = NULL,
               heartbeat_failure_count = 0,
               recovery_count = 0
         WHERE state IN ('generating', 'scoring')
           AND (
                job_name LIKE 'vegeta-prd-%%'
             OR job_name = 'inprocess'
           )
        """,
    )
    cleared = cr.rowcount
    if cleared:
        _logger.info(
            "[vegeta] upgrade to 19.0.2.5.0: cleared job_name and reset "
            "heartbeat_failure_count on %s in-flight job(s) so the new "
            "worker pool can re-claim them with a clean recovery counter.",
            cleared,
        )
    else:
        _logger.info(
            "[vegeta] upgrade to 19.0.2.5.0: no pre-existing in-flight "
            "PRD jobs to migrate.",
        )
