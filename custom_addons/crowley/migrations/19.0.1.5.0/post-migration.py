import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        ALTER TABLE crowley_generation
        ADD COLUMN IF NOT EXISTS golden_prompt TEXT,
        ADD COLUMN IF NOT EXISTS is_golden BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS review_status VARCHAR
    """)
    _logger.info("Crowley 19.0.1.5.0: ensured golden/review columns exist on crowley_generation")
