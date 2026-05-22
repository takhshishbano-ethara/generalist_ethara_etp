import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE crowley_video_review
        ADD COLUMN IF NOT EXISTS provider VARCHAR
    """)
    cr.execute("""
        UPDATE crowley_video_review
        SET provider = 'bedrock'
        WHERE provider IS NULL AND state = 'done'
    """)
    _logger.info(
        "Crowley 19.0.1.9.0: added provider column to crowley_video_review; "
        "review_client now supports openrouter (text-only) + bedrock (vision)."
    )
