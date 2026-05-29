"""Pre-migration for 19.0.6.0.0: add observability + recovery columns.

Three columns added for the 2026-05-27 pass:

1. ``current_phase`` (varchar) — sub-step within a state, surfaced in the
   ``stage_progress_html`` widget. Driven by ``_PHASE_LABELS`` in the
   model. Always-non-NULL with default ``''`` so the model code can
   ``write({"current_phase": ...})`` without worrying about NULL handling.

2. ``lambda_request_id`` (varchar, partial-indexed) — AWS Lambda RequestId
   from the most recent invoke. Used by the CloudWatch log fetch to
   filter log group events down to this job.

3. ``last_lambda_log_ts`` (timestamp) — watermark for the CloudWatch
   pagination loop. NULL on first fetch; updated to the most recent
   event timestamp on every successful pull so we don't re-ingest old
   events.

4. ``heartbeat_failure_count`` (integer, NOT NULL DEFAULT 0) — counts
   consecutive heartbeat-write failures for the row. Used by the
   two-gate reconcile in ``_prd_queue_recover_stale`` to distinguish
   "worker dead 15+ min" (unconditional gate) from "worker alive but
   heartbeat writes are failing" (short-stale + failures gate). The
   single-gate "stale heartbeat alone" check caused a documented
   double-Bedrock-spend incident in vegeta; this column is the fix
   ported into leviathan before the next flag-ON stage test.

Adding the columns at the SQL layer (instead of letting Odoo's ORM
do it at registry-load time) avoids ``UndefinedColumn`` errors during
the very-first read in load order — the cron may fire before the ORM
finishes adding the column.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        ALTER TABLE leviathan_job
          ADD COLUMN IF NOT EXISTS current_phase VARCHAR NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS lambda_request_id VARCHAR,
          ADD COLUMN IF NOT EXISTS last_lambda_log_ts TIMESTAMP,
          ADD COLUMN IF NOT EXISTS heartbeat_failure_count INTEGER NOT NULL DEFAULT 0
        """
    )
    # Partial index on lambda_request_id — CloudWatch fetch path looks up
    # jobs by RequestId. Most rows have NULL request_id (staged-only jobs,
    # historical pre-19.0.6 rows), so partial keeps the index small.
    cr.execute(
        """
        CREATE INDEX IF NOT EXISTS leviathan_job_lambda_request_id_idx
          ON leviathan_job (lambda_request_id)
         WHERE lambda_request_id IS NOT NULL
        """
    )
    _logger.info(
        "[leviathan] 19.0.6.0.0: added current_phase, lambda_request_id, "
        "last_lambda_log_ts, heartbeat_failure_count columns + partial "
        "index on leviathan_job"
    )
