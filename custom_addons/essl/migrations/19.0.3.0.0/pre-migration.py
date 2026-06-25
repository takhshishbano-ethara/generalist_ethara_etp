# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)

_LEGACY_COLUMNS = (
    "db_host", "db_port", "db_name", "db_username", "db_password", "db_timeout",
    "table_name", "user_id_column", "timestamp_column",
    "inout_column", "device_column", "device_filter_value",
)


def _legacy_table_exists(cr):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'essl_device'"
    )
    return bool(cr.fetchone())


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def _ensure_essl_server_table(cr):
    cr.execute("""
        CREATE TABLE IF NOT EXISTS essl_server (
            id                       SERIAL PRIMARY KEY,
            name                     VARCHAR,
            active                   BOOLEAN DEFAULT TRUE,
            db_host                  VARCHAR,
            db_port                  INTEGER DEFAULT 1433,
            db_name                  VARCHAR,
            db_username              VARCHAR,
            db_password              VARCHAR,
            db_timeout               INTEGER DEFAULT 10,
            devices_table            VARCHAR DEFAULT 'Devices',
            logs_table               VARCHAR DEFAULT 'DeviceLogs',
            logs_user_id_column      VARCHAR DEFAULT 'UserId',
            logs_timestamp_column    VARCHAR DEFAULT 'LogDate',
            logs_direction_column    VARCHAR DEFAULT 'Direction',
            logs_device_column       VARCHAR DEFAULT 'DeviceId',
            last_device_sync_at      TIMESTAMP,
            last_device_sync_count   INTEGER DEFAULT 0,
            last_device_sync_error   TEXT,
            create_uid               INTEGER,
            create_date              TIMESTAMP DEFAULT NOW(),
            write_uid                INTEGER,
            write_date               TIMESTAMP DEFAULT NOW()
        )
    """)


def _add_new_essl_device_columns(cr):
    new_columns = (
        ("server_id",              "INTEGER REFERENCES essl_server(id) ON DELETE CASCADE"),
        ("external_device_id",     "INTEGER"),
        ("short_name",             "VARCHAR"),
        ("device_location",        "VARCHAR"),
        ("serial_number",          "VARCHAR"),
        ("ip_address",             "VARCHAR"),
        ("connection_type",        "VARCHAR"),
        ("direction_mode",         "VARCHAR"),
        ("device_type",            "VARCHAR"),
        ("face_device_type",       "VARCHAR"),
        ("timezone_minutes",       "INTEGER"),
        ("activation_code",        "VARCHAR"),
        ("last_ping_at",           "TIMESTAMP"),
        ("last_log_download_at",   "TIMESTAMP"),
    )
    for col, ddl in new_columns:
        cr.execute(
            "ALTER TABLE essl_device ADD COLUMN IF NOT EXISTS %s %s" % (col, ddl)
        )


def _snapshot_legacy_rows(cr):
    available = [c for c in _LEGACY_COLUMNS if _column_exists(cr, "essl_device", c)]
    if not available:
        return []
    cols_sql = ", ".join(available)
    cr.execute(
        "SELECT id, %s FROM essl_device ORDER BY id" % cols_sql
    )
    rows = cr.fetchall()
    return [
        dict(zip(["id"] + available, row))
        for row in rows
    ]


def _create_servers_from_snapshot(cr, snapshot):
    key_to_server_id = {}
    for entry in snapshot:
        host = (entry.get("db_host") or "").strip()
        port = int(entry.get("db_port") or 1433)
        db = (entry.get("db_name") or "").strip()
        user = (entry.get("db_username") or "").strip()
        if not (host or db or user):
            continue
        key = (host, port, db, user)
        if key in key_to_server_id:
            continue
        cr.execute(
            "SELECT id FROM essl_server "
            "WHERE db_host = %s AND db_port = %s "
            "AND db_name = %s AND db_username = %s LIMIT 1",
            (host, port or 1433, db, user),
        )
        existing = cr.fetchone()
        if existing:
            key_to_server_id[key] = existing[0]
            continue
        cr.execute("""
            INSERT INTO essl_server (
                name, active, db_host, db_port, db_name, db_username, db_password, db_timeout,
                devices_table, logs_table,
                logs_user_id_column, logs_timestamp_column,
                logs_direction_column, logs_device_column,
                create_uid, write_uid
            ) VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, 1, 1)
            RETURNING id
        """, (
            "%s / %s" % (host or "?", db or "?"),
            host, port or 1433, db, user,
            entry.get("db_password") or "",
            int(entry.get("db_timeout") or 10),
            "Devices",
            entry.get("table_name") or "DeviceLogs",
            entry.get("user_id_column") or "UserId",
            entry.get("timestamp_column") or "LogDate",
            entry.get("inout_column") or "Direction",
            entry.get("device_column") or "DeviceId",
        ))
        key_to_server_id[key] = cr.fetchone()[0]
    return key_to_server_id


def _link_devices_to_servers(cr, snapshot, key_to_server_id):
    fallback_server_id = next(iter(key_to_server_id.values()), None)
    for entry in snapshot:
        device_id = entry["id"]
        host = (entry.get("db_host") or "").strip()
        port = int(entry.get("db_port") or 1433)
        db = (entry.get("db_name") or "").strip()
        user = (entry.get("db_username") or "").strip()
        server_id = key_to_server_id.get((host, port, db, user)) or fallback_server_id

        dev_val = entry.get("device_filter_value")
        ext_id = None
        if dev_val is not None:
            stripped = str(dev_val).strip()
            if stripped.lstrip("-").isdigit():
                ext_id = int(stripped)
        if ext_id is None:
            ext_id = -device_id

        cr.execute("""
            UPDATE essl_device
               SET server_id = %s,
                   external_device_id = %s,
                   sync_enabled = CASE WHEN %s < 0 THEN FALSE ELSE sync_enabled END,
                   last_error = CASE WHEN %s < 0
                                     THEN 'MIGRATED: legacy catch-all device. Run /essl/api/pull-devices and reassign.'
                                     ELSE last_error END
             WHERE id = %s
        """, (server_id, ext_id, ext_id, ext_id, device_id))


def migrate(cr, version):
    if not version:
        return
    if not _legacy_table_exists(cr):
        _logger.info("ESSL pre-migration: essl_device table absent, fresh install.")
        return
    _ensure_essl_server_table(cr)
    _add_new_essl_device_columns(cr)

    snapshot = _snapshot_legacy_rows(cr)
    if not snapshot:
        _logger.info("ESSL pre-migration: no legacy rows to migrate.")
        return

    key_to_server_id = _create_servers_from_snapshot(cr, snapshot)
    if not key_to_server_id:
        _logger.warning("ESSL pre-migration: no usable connection found in legacy rows.")
        return
    _link_devices_to_servers(cr, snapshot, key_to_server_id)
    _logger.info(
        "ESSL pre-migration: linked %d device rows to %d server(s).",
        len(snapshot), len(key_to_server_id),
    )
