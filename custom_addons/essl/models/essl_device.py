# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EsslDevice(models.Model):
    _name = "essl.device"
    _description = "ESSL Biometric Device"
    _order = "server_id, name"

    server_id = fields.Many2one(
        "essl.server", string="ESSL Server", required=True,
        ondelete="cascade", index=True,
    )
    external_device_id = fields.Integer(
        string="Device ID (Source)", required=True, index=True,
        help="The DeviceId value from dbo.Devices on the eSSL SQL Server.",
    )

    name = fields.Char(string="Device Name", required=True)
    short_name = fields.Char(string="Short Name")
    location = fields.Char(string="Odoo Location Note")
    active = fields.Boolean(default=True)
    sync_enabled = fields.Boolean(
        string="Pull API Enabled", default=True,
        help=(
            "If unchecked, this device is skipped by the bulk /essl/api/pull "
            "endpoint and by the periodic cron."
        ),
    )

    serial_number = fields.Char(string="Serial Number", readonly=True)
    ip_address = fields.Char(string="IP Address", readonly=True)
    connection_type = fields.Char(string="Connection Type", readonly=True)
    direction_mode = fields.Char(string="Direction Mode", readonly=True)
    device_type = fields.Char(string="Device Type", readonly=True)
    device_location = fields.Char(string="Device Location (Source)", readonly=True)
    face_device_type = fields.Char(string="Face Device Type", readonly=True)
    timezone_minutes = fields.Integer(string="Timezone (minutes)", readonly=True)
    activation_code = fields.Char(string="Activation Code", readonly=True)
    last_ping_at = fields.Datetime(string="Last Ping (Source)", readonly=True)
    last_log_download_at = fields.Datetime(string="Last Log Download (Source)", readonly=True)

    last_sync_time = fields.Datetime(string="Last Sync At")
    last_sync_count = fields.Integer(string="Last Sync Count", default=0)
    sync_status = fields.Selection(
        [("idle", "Idle"), ("syncing", "Syncing"), ("error", "Error")],
        default="idle",
    )
    last_error = fields.Text(string="Last Error")

    log_ids = fields.One2many("essl.attendance.log", "device_id", string="Attendance Logs")
    log_count = fields.Integer(string="Log Count", compute="_compute_log_count")

    _uniq_server_external = models.Constraint(
        "unique(server_id, external_device_id)",
        "Each source DeviceId can appear only once per ESSL server.",
    )

    @api.depends("log_ids")
    def _compute_log_count(self):
        data = self.env["essl.attendance.log"].read_group(
            [("device_id", "in", self.ids)], ["device_id"], ["device_id"]
        )
        count_map = {row["device_id"][0]: row["device_id_count"] for row in data}
        for device in self:
            device.log_count = count_map.get(device.id, 0)

    def action_view_attendance_logs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Attendance Logs",
            "res_model": "essl.attendance.log",
            "view_mode": "list,form",
            "domain": [("device_id", "=", self.id)],
            "context": {"default_device_id": self.id},
        }

    def action_test_connection(self):
        self.ensure_one()
        if not self.server_id:
            raise UserError(_("This device has no ESSL Server assigned."))
        return self.server_id.action_test_connection()

    def action_sync_now(self):
        self.ensure_one()
        result = self._pull_attendance()
        msg = _("Pulled %(created)d new punches; %(processed)d processed; %(failed)d failed.") % result
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("ESSL Sync"), "message": msg, "sticky": False, "type": "success"},
        }

    def _interpret_direction(self, value):
        if value is None:
            return None
        s = str(value).strip().upper()
        if s in ("0", "I", "IN"):
            return "0"
        if s in ("1", "O", "OUT"):
            return "1"
        if s == "4":
            return "4"
        if s == "5":
            return "5"
        return None

    def _build_pull_query(self):
        self.ensure_one()
        srv = self.server_id
        srv._validate_identifier(srv.logs_table, "Logs Table")
        srv._validate_identifier(srv.logs_user_id_column, "Logs User ID Column")
        srv._validate_identifier(srv.logs_timestamp_column, "Logs Timestamp Column")
        srv._validate_identifier(srv.logs_device_column, "Logs Device FK Column")
        cols = [
            "[%s] AS user_id" % srv.logs_user_id_column,
            "[%s] AS punch_time" % srv.logs_timestamp_column,
        ]
        if srv.logs_direction_column:
            srv._validate_identifier(srv.logs_direction_column, "Logs Direction Column")
            cols.append("[%s] AS direction" % srv.logs_direction_column)
        sql = "SELECT %s FROM [%s] WHERE [%s] > ? AND [%s] = ?" % (
            ", ".join(cols),
            srv.logs_table,
            srv.logs_timestamp_column,
            srv.logs_device_column,
        )
        params = [
            self.last_sync_time or (fields.Datetime.now() - timedelta(days=1)),
            self.external_device_id,
        ]
        sql += " ORDER BY [%s] ASC" % srv.logs_timestamp_column
        return sql, tuple(params)

    def _pull_attendance(self, conn=None, employee_map=None):
        self.ensure_one()
        if not self.server_id:
            raise UserError(_("Device '%s' has no ESSL Server assigned.") % self.display_name)
        created = 0
        processed = 0
        failed = 0
        owns_conn = conn is None
        if owns_conn:
            conn = self.server_id._connect()
        try:
            cursor = conn.cursor()
            sql, params = self._build_pull_query()
            cursor.execute(sql, params)
            raw_rows = cursor.fetchall()
            cols = [col[0] for col in cursor.description]
            rows = [dict(zip(cols, r)) for r in raw_rows]
            cursor.close()
            _logger.info("ESSL %s: fetched %d raw rows", self.name, len(rows))
        except UserError:
            raise
        except Exception as exc:
            self.sudo().write({"sync_status": "error", "last_error": str(exc)[:1000]})
            raise UserError(_("Query failed: %s") % exc)
        finally:
            if owns_conn:
                try:
                    conn.close()
                except Exception:
                    pass

        Log = self.env["essl.attendance.log"]
        if employee_map is None:
            employee_map = Log.build_employee_map()

        existing_keys = set()
        if rows:
            self.env.cr.execute(
                "SELECT device_user_id, punch_timestamp "
                "FROM essl_attendance_log "
                "WHERE device_id = %s AND punch_timestamp >= %s",
                (self.id, min(r["punch_time"] for r in rows if r.get("punch_time"))),
            )
            existing_keys = {(uid, ts) for uid, ts in self.env.cr.fetchall()}

        new_vals = []
        max_ts = self.last_sync_time
        for row in rows:
            user_id_val = row.get("user_id")
            punch_time = row.get("punch_time")
            if user_id_val is None or punch_time is None:
                continue
            user_id = str(user_id_val)
            if (user_id, punch_time) in existing_keys:
                continue
            if self.server_id.logs_direction_column:
                direction = self._interpret_direction(row.get("direction"))
                if not direction:
                    continue
            else:
                direction = "0"
            if not max_ts or punch_time > max_ts:
                max_ts = punch_time
            new_vals.append({
                "device_id": self.id,
                "device_user_id": user_id,
                "punch_timestamp": punch_time,
                "status": direction,
            })

        if new_vals:
            new_logs = Log.create(new_vals)
            created = len(new_logs)
            for log in new_logs:
                try:
                    Log.process_log(log.id, employee_map=employee_map)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    _logger.warning("ESSL: process_log failed for log %d: %s", log.id, exc)

        self.sudo().write({
            "last_sync_time": max_ts or self.last_sync_time,
            "last_sync_count": created,
            "sync_status": "idle",
            "last_error": False,
        })
        _logger.info(
            "ESSL %s: created=%d processed=%d failed=%d",
            self.name, created, processed, failed,
        )
        return {"created": created, "processed": processed, "failed": failed}

    @api.model
    def _cron_pull_all_devices(self):
        domain = [("active", "=", True), ("sync_enabled", "=", True), ("server_id.active", "=", True)]
        for device in self.search(domain):
            try:
                device._pull_attendance()
            except Exception as exc:
                device.sudo().write({"sync_status": "error", "last_error": str(exc)[:1000]})
                _logger.exception("ESSL: cron pull failed for %s", device.display_name)
