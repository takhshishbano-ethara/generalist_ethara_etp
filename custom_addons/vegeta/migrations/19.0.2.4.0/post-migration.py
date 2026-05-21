"""Post-migration for the K8s-Job-per-job PRD architecture (19.0.2.4.0).

``job_name`` is a plain ``fields.Char`` — Odoo's ORM creates its column and
index automatically during ``-u vegeta``, so no DDL is needed here.

This script carries the rolling-deploy double-run guard. Every job already
in ``generating``/``scoring`` at upgrade time is stamped with the in-process
sentinel ``job_name`` so the new PRD dispatch cron skips it: its pre-upgrade
in-process thread finishes the job, or — if that thread was killed by the
rolling deploy — the 3 h watchdog backstop catches it. Handling this once,
here, lets the dispatch cron treat an empty ``job_name`` as the unambiguous
"new, never dispatched" signal and dispatch within one cron tick.
"""

import logging

_logger = logging.getLogger(__name__)

# Must match models.vegeta_job._INPROCESS_JOB_NAME — kept as a literal because
# importing the model module during migration is fragile.
_INPROCESS_JOB_NAME = "inprocess"


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE vegeta_job
           SET job_name = %s
         WHERE state IN ('generating', 'scoring')
           AND (job_name IS NULL OR job_name = '')
        """,
        (_INPROCESS_JOB_NAME,),
    )
    stamped = cr.rowcount
    if stamped:
        _logger.info(
            "[vegeta] upgrade to 19.0.2.4.0: stamped %s in-flight PRD job(s) "
            "with the in-process sentinel so the dispatch cron will not "
            "re-run them.",
            stamped,
        )
    else:
        _logger.info("[vegeta] upgrade to 19.0.2.4.0: no in-flight PRD jobs.")
