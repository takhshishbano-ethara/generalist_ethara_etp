import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Drop the orphan wiki_leave_policy table left behind when the
    wiki.leave.policy model was removed (the Leave Policy page is served from
    wiki.document.page, page_key='leave_policy'). Odoo never auto-drops tables
    for deleted models, so clean it up here."""
    cr.execute("DROP TABLE IF EXISTS wiki_leave_policy CASCADE")
    _logger.info("wiki_core: dropped orphan wiki_leave_policy table")
