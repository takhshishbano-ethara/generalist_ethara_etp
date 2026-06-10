import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ir.config_parameter"].sudo().set_param("i2i.llm_auto_run", "False")
    _logger.info(
        "i2i 19.0.1.10.0 migration: forced i2i.llm_auto_run = 'False' "
        "per Recommended Flow (Documentation Aligned)."
    )
