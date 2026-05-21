# -*- coding: utf-8 -*-
"""Module hooks.

Defines a post-init/migration hook that backfills attempt #1 for every
existing job during an upgrade from the v1.5 single-attempt schema to
the v1.6 job + attempts schema. Without this hook, existing jobs would
look empty (no attempts → no videos) after the upgrade.

The hook is idempotent — running it again on a database that already
has attempts is a no-op.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate_jobs_to_attempts(env):
    """Backfill attempt #1 from each job's legacy fields.

    Skips any job that already has at least one attempt. Reads the raw
    legacy columns directly via SQL because Odoo's ORM no longer
    exposes them (they were removed from the Python model when they
    moved to the attempt schema, but the columns still exist in
    Postgres until the ORM drops them — Odoo will only drop them after
    this hook has done its work).
    """
    cr = env.cr
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'crowley_ai_vid_gen_job'
          AND column_name IN (
            'state', 'openrouter_job_id', 'openrouter_polling_url',
            'poll_attempts', 'last_poll_at', 's3_bucket', 's3_key',
            's3_etag', 'source_sha256', 'file_size', 'mimetype',
            'cost_usd', 'error_message', 'submitted_at', 'completed_at',
            'webhook_idempotency_key', 'seed', 'prompt'
          )
        """
    )
    available = {row[0] for row in cr.fetchall()}
    legacy_cols = (
        "id", "user_id", "prompt", "seed",
        "state", "openrouter_job_id", "openrouter_polling_url",
        "poll_attempts", "last_poll_at", "s3_bucket", "s3_key",
        "s3_etag", "source_sha256", "file_size", "mimetype",
        "cost_usd", "error_message", "submitted_at", "completed_at",
        "webhook_idempotency_key",
    )
    selectable = [c for c in legacy_cols if c == "id" or c in available]
    if "state" not in available:
        _logger.info(
            "Crowley migrate: legacy 'state' column not on jobs table — "
            "fresh install, no migration needed."
        )
        return

    cr.execute(f"SELECT {', '.join(selectable)} FROM crowley_ai_vid_gen_job")
    rows = cr.dictfetchall()
    if not rows:
        _logger.info("Crowley migrate: no existing jobs, nothing to backfill.")
        return

    cr.execute("SELECT DISTINCT job_id FROM crowley_ai_vid_gen_attempt")
    jobs_with_attempts = {row[0] for row in cr.fetchall()}

    created = 0
    skipped = 0
    Attempt = env["crowley.ai.vid.gen.attempt"].sudo()
    for row in rows:
        if row["id"] in jobs_with_attempts:
            skipped += 1
            continue
        legacy_state = row.get("state") or "draft"
        if legacy_state == "draft":
            skipped += 1
            continue
        vals = {
            "job_id": row["id"],
            "attempt_number": 1,
            "prompt": row.get("prompt") or "",
            "seed": row.get("seed") or False,
            "state": legacy_state,
            "openrouter_job_id": row.get("openrouter_job_id") or False,
            "openrouter_polling_url": row.get("openrouter_polling_url") or False,
            "poll_attempts": row.get("poll_attempts") or 0,
            "last_poll_at": row.get("last_poll_at"),
            "s3_bucket": row.get("s3_bucket") or False,
            "s3_key": row.get("s3_key") or False,
            "s3_etag": row.get("s3_etag") or False,
            "source_sha256": row.get("source_sha256") or False,
            "file_size": row.get("file_size") or 0,
            "mimetype": row.get("mimetype") or "video/mp4",
            "cost_usd": row.get("cost_usd") or 0.0,
            "error_message": row.get("error_message") or False,
            "submitted_at": row.get("submitted_at"),
            "completed_at": row.get("completed_at"),
            "webhook_idempotency_key": row.get("webhook_idempotency_key") or False,
        }
        Attempt.create(vals)
        created += 1
    _logger.info(
        "Crowley migrate: backfilled %d attempt #1 rows (skipped %d).",
        created, skipped,
    )


def repoint_cron_to_attempt(env):
    """Re-aim the polling cron at the new ``crowley.ai.vid.gen.attempt`` model.

    The cron is declared in ``data/ir_cron.xml`` with ``noupdate="1"``,
    so Odoo will not overwrite the existing row on upgrade. Until this
    heals itself, a v1.5 → v1.6 upgrade leaves the cron pointing at
    the (now lifecycle-method-less) job model and every tick errors
    with ``AttributeError: 'crowley.ai.vid.gen.job' object has no
    attribute '_cron_poll_openrouter'``.

    Idempotent: writing the same model id again is harmless.
    """
    cron = env.ref("crowley_ai_vid_gen.cron_crowley_poll_openrouter",
                   raise_if_not_found=False)
    if not cron:
        _logger.info("Crowley migrate: cron not present, nothing to repoint.")
        return
    target = env["ir.model"].sudo()._get("crowley.ai.vid.gen.attempt")
    if not target:
        _logger.warning(
            "Crowley migrate: attempt model not yet registered; skipping cron repoint."
        )
        return
    server_action = cron.sudo().ir_actions_server_id
    if server_action and server_action.model_id.id != target.id:
        server_action.sudo().write({"model_id": target.id})
        _logger.info(
            "Crowley migrate: repointed cron server action %s to model_id=%s",
            server_action.id, target.id,
        )


def post_init_hook(env):
    migrate_jobs_to_attempts(env)
    repoint_cron_to_attempt(env)
