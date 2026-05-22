import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE crowley_video_review
        ADD COLUMN IF NOT EXISTS bedrock_request_id VARCHAR
    """)
    _logger.info("Crowley 19.0.1.6.0: ensured bedrock_request_id exists on crowley_video_review")
