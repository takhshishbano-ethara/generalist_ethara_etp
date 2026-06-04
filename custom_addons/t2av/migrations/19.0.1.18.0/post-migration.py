import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        SELECT value FROM ir_config_parameter
         WHERE key = 't2av.enable_gemini_qc'
         LIMIT 1
        """
    )
    existing = cr.fetchone()
    if existing is None:
        cr.execute(
            """
            SELECT 1 FROM t2av_video_review
             WHERE provider IN ('bedrock', 'openrouter')
             LIMIT 1
            """
        )
        has_legacy = cr.fetchone() is not None
        default_value = 'True' if has_legacy else 'False'
        cr.execute(
            """
            INSERT INTO ir_config_parameter (key, value, create_date, write_date)
            VALUES ('t2av.enable_gemini_qc', %s, NOW() AT TIME ZONE 'UTC',
                    NOW() AT TIME ZONE 'UTC')
            """,
            (default_value,),
        )
        _logger.info(
            "T2AV 19.0.1.18.0: initialised t2av.enable_gemini_qc=%s "
            "(legacy reviews present: %s).",
            default_value, has_legacy,
        )
    else:
        _logger.info(
            "T2AV 19.0.1.18.0: t2av.enable_gemini_qc already set to %r; left unchanged.",
            existing[0],
        )

    cr.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_t2av_review_stack_queue
            ON t2av_video_review (create_date, id)
         WHERE provider = 'human'
           AND state = 'queued'
           AND assigned_to_id IS NULL
        """
    )
    _logger.info(
        "T2AV 19.0.1.18.0: ensured partial index idx_t2av_review_stack_queue "
        "for Stack-wizard claim query."
    )
