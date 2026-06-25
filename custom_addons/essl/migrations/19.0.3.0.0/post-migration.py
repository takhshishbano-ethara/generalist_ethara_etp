# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)

_LEGACY_COLUMNS = (
    "db_host", "db_port", "db_name", "db_username", "db_password", "db_timeout",
    "table_name", "user_id_column", "timestamp_column",
    "inout_column", "device_column", "device_filter_value",
)


def migrate(cr, version):
    if not version:
        return
    for col in _LEGACY_COLUMNS:
        cr.execute("ALTER TABLE essl_device DROP COLUMN IF EXISTS %s" % col)
    _logger.info("ESSL post-migration: dropped legacy columns from essl_device.")
