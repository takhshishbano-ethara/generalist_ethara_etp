"""Post-migration hook for T2AV v1.1.

Backfills existing t2av_generation rows into the new job/attempt schema.
After v1.0 each t2av_generation row owned all the per-attempt fields
(state, openrouter_job_id, video_s3_url, ...). v1.1 splits these onto a
new t2av_attempt child table. For each existing non-draft job, create
attempt #1 mirroring the old fields.

Idempotent: skips jobs that already have an attempt.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info("T2AV: post-migration to 19.0.1.1.0 starting")
    _backfill_attempts_from_legacy_jobs(cr)
    _logger.info("T2AV: post-migration complete")


def _backfill_attempts_from_legacy_jobs(cr):
    # Detect whether the legacy single-model columns still exist on
    # t2av_generation. If they were dropped already (clean install), nothing
    # to backfill.
    cr.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 't2av_generation'
        AND column_name IN ('openrouter_job_id', 'video_s3_url', 'state')
    """)
    legacy_cols = {row[0] for row in cr.fetchall()}
    if 'state' not in legacy_cols:
        _logger.info("T2AV: no legacy state column; clean install, skipping backfill")
        return

    # Find every job that needs backfilling: non-draft AND has no attempts yet.
    cr.execute("""
        SELECT g.id
        FROM t2av_generation g
        LEFT JOIN t2av_attempt a ON a.job_id = g.id
        WHERE g.state != 'draft'
          AND a.id IS NULL
    """)
    job_ids = [row[0] for row in cr.fetchall()]
    if not job_ids:
        _logger.info("T2AV: no jobs to backfill")
        return

    _logger.info("T2AV: backfilling %d job(s) into attempt 1", len(job_ids))

    # Build the INSERT dynamically based on which legacy columns are present
    # (so the migration works even if Odoo has dropped some columns).
    field_map = [
        # (attempt_column, legacy_generation_column, default)
        ('prompt', 'prompt', "''"),
        ('state', 'state', "'draft'"),
        ('openrouter_job_id', 'openrouter_job_id', 'NULL'),
        ('openrouter_polling_url', 'openrouter_polling_url', 'NULL'),
        ('openrouter_status', 'openrouter_status', 'NULL'),
        ('poll_attempts', 'poll_attempts', '0'),
        ('last_polled_at', 'last_polled_at', 'NULL'),
        ('error_message', 'error_message', 'NULL'),
        ('error_code', 'error_code', 'NULL'),
        ('submitted_at', 'submitted_at', 'NULL'),
        ('completed_at', 'completed_at', 'NULL'),
        ('tokens_used', 'tokens_used', '0'),
        ('cost_usd', 'cost_usd', '0.0'),
        ('video_temporary_url', 'video_temporary_url', 'NULL'),
        ('video_expires_at', 'video_expires_at', 'NULL'),
        ('video_s3_key', 'video_s3_url', 'NULL'),  # legacy stored full URL; we'll keep it for now
        ('video_size_bytes', 'video_size_bytes', '0'),
        ('video_sha256', 'video_sha256', 'NULL'),
        ('raw_response_json', 'raw_response_json', 'NULL'),
    ]

    # Filter to columns that actually exist
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='t2av_generation'
    """)
    existing_legacy_cols = {row[0] for row in cr.fetchall()}

    select_cols = ['g.id']
    insert_cols = ['job_id', 'attempt_number', 'create_uid', 'create_date', 'write_uid', 'write_date']
    insert_select = ['g.id', '1', '1', 'NOW()', '1', 'NOW()']

    for attempt_col, legacy_col, default in field_map:
        if legacy_col in existing_legacy_cols:
            select_cols.append(f'g.{legacy_col}')
            insert_cols.append(attempt_col)
            insert_select.append(f'g.{legacy_col}')

    sql = f"""
        INSERT INTO t2av_attempt ({', '.join(insert_cols)})
        SELECT {', '.join(insert_select)}
        FROM t2av_generation g
        LEFT JOIN t2av_attempt a ON a.job_id = g.id
        WHERE g.id = ANY(%s) AND a.id IS NULL
    """
    cr.execute(sql, (job_ids,))
    _logger.info("T2AV: backfilled %d attempt(s)", cr.rowcount)
