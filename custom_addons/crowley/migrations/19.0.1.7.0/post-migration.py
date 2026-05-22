import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE crowley_enrichment
        ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6) DEFAULT 0.0
    """)
    cr.execute("""
        ALTER TABLE crowley_video_review
        ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6) DEFAULT 0.0
    """)
    _logger.info("Crowley 19.0.1.7.0: ensured cost_usd columns exist")
