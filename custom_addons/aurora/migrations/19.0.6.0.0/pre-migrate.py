import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'aurora_github_token'"
    )
    if not cr.fetchone():
        return
    cr.execute("""
        ALTER TABLE aurora_github_token
        DROP CONSTRAINT IF EXISTS aurora_github_token_leased_by_run_id_fkey
    """)
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'aurora_evaluation'"
    )
    if cr.fetchone():
        cr.execute("""
            ALTER TABLE aurora_evaluation
            ADD COLUMN IF NOT EXISTS custom_jsonl_file BYTEA
        """)
    _logger.info("Aurora 19.0.6.0.0: dropped token FK, added custom_jsonl_file column")
