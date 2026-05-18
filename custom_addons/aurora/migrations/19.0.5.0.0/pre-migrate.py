"""Aurora 19.0.5.0.0 pre-migration: add custom JSONL import fields to aurora_evaluation."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'aurora_evaluation'"
    )
    if not cr.fetchone():
        return
    cr.execute("""
        ALTER TABLE aurora_evaluation
        ADD COLUMN IF NOT EXISTS dataset_source VARCHAR DEFAULT 'pipeline'
    """)
    cr.execute("""
        UPDATE aurora_evaluation SET dataset_source = 'pipeline' WHERE dataset_source IS NULL
    """)
    cr.execute("""
        ALTER TABLE aurora_evaluation
        ADD COLUMN IF NOT EXISTS custom_org VARCHAR
    """)
    cr.execute("""
        ALTER TABLE aurora_evaluation
        ADD COLUMN IF NOT EXISTS custom_repo VARCHAR
    """)
    cr.execute("""
        ALTER TABLE aurora_evaluation
        ADD COLUMN IF NOT EXISTS custom_jsonl_filename VARCHAR
    """)
    _logger.info("Aurora 19.0.5.0.0: added custom JSONL import columns to aurora_evaluation")
