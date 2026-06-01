import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE video_editor_job
            ADD COLUMN IF NOT EXISTS lambda_request_id VARCHAR,
            ADD COLUMN IF NOT EXISTS last_lambda_log_ts TIMESTAMP
    """)
    cr.execute("""
        CREATE INDEX IF NOT EXISTS video_editor_job_lambda_request_id_idx
          ON video_editor_job (lambda_request_id)
         WHERE lambda_request_id IS NOT NULL
    """)
    _logger.info("[video_editor_s3] 19.0.1.0.20: added lambda_request_id + last_lambda_log_ts to video_editor_job")
