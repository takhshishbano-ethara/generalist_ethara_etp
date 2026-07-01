import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Drop the orphan wiki_holiday table left behind when the wiki.holiday
    model was removed (holidays now come from Time Off → Public Holidays).
    Odoo never auto-drops tables for deleted models, so clean it up here."""
    cr.execute("DROP TABLE IF EXISTS wiki_holiday CASCADE")
    _logger.info("wiki_core: dropped orphan wiki_holiday table")
