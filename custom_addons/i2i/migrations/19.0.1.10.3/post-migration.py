import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    removed = env["ir.config_parameter"].sudo().search(
        [("key", "=", "i2i.llm_auto_run")]
    )
    if removed:
        removed.unlink()
        _logger.info(
            "i2i 19.0.1.10.3 migration: removed obsolete "
            "i2i.llm_auto_run config parameter."
        )
    cr.execute(
        "UPDATE i2i_item SET state = 'human_qc' WHERE state = 'llm_qc'"
    )
    if cr.rowcount:
        _logger.info(
            "i2i 19.0.1.10.3 migration: migrated %s items from "
            "legacy 'llm_qc' state to 'human_qc'.",
            cr.rowcount,
        )
