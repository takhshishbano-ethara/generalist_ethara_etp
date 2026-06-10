import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("SELECT id FROM res_company ORDER BY id LIMIT 1")
    row = cr.fetchone()
    if not row:
        return
    default_company_id = row[0]
    cr.execute(
        "UPDATE i2i_item SET company_id = %s WHERE company_id IS NULL",
        (default_company_id,),
    )
    if cr.rowcount:
        _logger.info(
            "i2i 19.0.1.5.0 migration: backfilled company_id on %s rows",
            cr.rowcount,
        )
