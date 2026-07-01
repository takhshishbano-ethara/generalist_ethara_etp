import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        DELETE FROM hr_attendance
        WHERE id IN (
            SELECT DISTINCT attendance_id
            FROM essl_attendance_log
            WHERE attendance_id IS NOT NULL
        )
    """)
    deleted_attendance = cr.rowcount
    cr.execute("DELETE FROM essl_attendance_log")
    deleted_logs = cr.rowcount
    cr.execute("""
        UPDATE essl_device
        SET last_sync_time = NULL,
            last_sync_count = 0,
            sync_status = 'idle',
            last_error = NULL
    """)
    reset_devices = cr.rowcount
    _logger.warning(
        "ESSL 19.0.3.5.0 pre-migration (timezone fix): "
        "wiped %d hr_attendance, %d essl_attendance_log, "
        "reset %d essl_device sync cursors",
        deleted_attendance, deleted_logs, reset_devices,
    )
