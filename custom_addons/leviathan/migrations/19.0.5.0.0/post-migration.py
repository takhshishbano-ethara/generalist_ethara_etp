"""Post-migration for the Phase-2 PRD queue (19.0.5.0.0).

The four new columns (`prd_queued_at`, `prd_claim_count`, `prd_failure_count`,
`pipeline_status`) are created automatically by Odoo's ORM during `-u leviathan`.
This script only does the minimal data backfill needed so the drainer's
ordering and poison logic do not misbehave on legacy rows.

Behaviour-neutral:
- The feature flag `leviathan.prd_queue_enabled` defaults to False, so the
  drainer is a no-op after this upgrade unless an operator flips it.
- `prd_claim_count` / `prd_failure_count` default to 0 — no backfill needed.
- `pipeline_status` is informational only — no backfill needed.

Only `prd_queued_at` benefits from a one-shot backfill so an in-flight
`generating` row at upgrade time has a stable FIFO position when the flag
is eventually flipped on.

See docs/LEVIATHAN_POD_ARCHITECTURE.md §3.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE leviathan_job
           SET prd_queued_at = COALESCE(prd_queued_at, started_at, write_date)
         WHERE state = 'generating' AND prd_queued_at IS NULL
        """
    )
    backfilled = cr.rowcount
    if backfilled:
        _logger.info(
            "[leviathan] migration 19.0.5.0.0: backfilled prd_queued_at on "
            "%d in-flight `generating` row(s) for clean FIFO ordering once "
            "the queue is enabled.",
            backfilled,
        )
    else:
        _logger.info(
            "[leviathan] migration 19.0.5.0.0: no in-flight `generating` "
            "rows; nothing to backfill."
        )
