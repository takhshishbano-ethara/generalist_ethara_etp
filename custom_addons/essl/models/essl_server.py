# -*- coding: utf-8 -*-
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_IST = ZoneInfo("Asia/Kolkata")
_ATTENDANCE_TIME_BUDGET_MS = 110_000


def _ist_to_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=_IST).astimezone(timezone.utc).replace(tzinfo=None)

_logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Tried in order. Driver 17 (and 13) don't suffer from the OpenSSL 3
# "legacy sigalg disallowed" handshake failure that Driver 18 hits when
# the SQL Server presents a SHA-1 / legacy self-signed certificate.
_DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
)

# Substrings that indicate a TLS/sigalg handshake failure worth retrying
# with an older driver. Anything else (auth, network, db missing) is fatal
# and must surface immediately.
_TLS_FALLBACK_MARKERS = (
    "legacy sigalg",
    "SSL Provider",
    "SSL routines",
    "ssl handshake",
    "TLS",
)

DEVICE_SOURCE_COLUMNS = (
    "DeviceId",
    "DeviceFName",
    "DeviceSName",
    "DeviceDirection",
    "SerialNumber",
    "ConnectionType",
    "IpAddress",
    "LastPing",
    "LastLogDownloadDate",
    "DeviceType",
    "DeviceLocation",
    "Timezone",
    "FaceDeviceType",
    "DeviceActivationCode",
)


class EsslServer(models.Model):
    _name = "essl.server"
    _description = "ESSL SQL Server (eTimeTrackLite database)"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(default=True)

    db_host = fields.Char(string="Server Host", required=True)
    db_port = fields.Integer(string="Port", default=1433)
    db_name = fields.Char(string="Database Name", required=True)
    db_username = fields.Char(string="Username", required=True)
    db_password = fields.Char(string="Password")
    db_timeout = fields.Integer(string="Timeout (s)", default=10)

    devices_table = fields.Char(
        string="Devices Table", default="Devices", required=True,
        help="Source-of-truth master device table on the eSSL SQL Server.",
    )
    logs_table = fields.Char(
        string="Logs Table", default="DeviceLogs", required=True,
    )
    logs_user_id_column = fields.Char(
        string="Logs: User ID Column", default="UserId", required=True,
    )
    logs_timestamp_column = fields.Char(
        string="Logs: Timestamp Column", default="LogDate", required=True,
    )
    logs_direction_column = fields.Char(
        string="Logs: Direction Column", default="Direction",
        help="Optional. If blank, every punch is treated as a check-in.",
    )
    logs_device_column = fields.Char(
        string="Logs: Device FK Column", default="DeviceId", required=True,
        help="Foreign-key column in the logs table that joins back to Devices.",
    )
    attendance_logs_table = fields.Char(
        string="Attendance Logs Table", default="AttendanceLogs", required=True,
    )
    employees_table = fields.Char(
        string="Employees Table", default="Employees", required=True,
    )

    last_device_sync_at = fields.Datetime(string="Last Device Sync At", readonly=True)
    last_device_sync_count = fields.Integer(string="Devices Touched (last run)", readonly=True)
    last_device_sync_error = fields.Text(string="Last Device Sync Error", readonly=True)

    device_ids = fields.One2many("essl.device", "server_id", string="Devices")
    device_count = fields.Integer(string="Device Count", compute="_compute_device_count")

    @api.depends("device_ids")
    def _compute_device_count(self):
        data = self.env["essl.device"].read_group(
            [("server_id", "in", self.ids)], ["server_id"], ["server_id"]
        )
        count_map = {row["server_id"][0]: row["server_id_count"] for row in data}
        for srv in self:
            srv.device_count = count_map.get(srv.id, 0)

    def _validate_identifier(self, name, label):
        if not name or not _IDENT_RE.match(name):
            raise UserError(_("Invalid identifier for %s: %r") % (label, name))

    def _connect(self):
        self.ensure_one()
        try:
            import pyodbc  # noqa: PLC0415
        except ImportError:
            raise UserError(_(
                "pyodbc is not installed on the Odoo server. "
                "Install it with: pip install pyodbc (also requires ODBC Driver 18 for SQL Server)"
            ))

        installed = {d.strip() for d in pyodbc.drivers()}
        candidates = [d for d in _DRIVER_PREFERENCE if d in installed]
        if not candidates:
            raise UserError(_(
                "No supported SQL Server ODBC driver found on the Odoo host. "
                "Installed drivers: %s. Install msodbcsql18 (or 17)."
            ) % (", ".join(sorted(installed)) or _("none")))

        timeout = self.db_timeout or 10
        last_exc = None
        for driver in candidates:
            conn_str = (
                "DRIVER={%s};"
                "SERVER=%s,%d;"
                "DATABASE=%s;"
                "UID=%s;"
                "PWD=%s;"
                "Encrypt=no;"
                "TrustServerCertificate=yes;"
                "Connection Timeout=%d;"
            ) % (
                driver,
                self.db_host,
                self.db_port or 1433,
                self.db_name,
                self.db_username,
                self.db_password or "",
                timeout,
            )
            try:
                conn = pyodbc.connect(conn_str, timeout=timeout)
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if any(marker in msg for marker in _TLS_FALLBACK_MARKERS):
                    _logger.warning(
                        "ESSL %s: driver %r failed TLS handshake (%s); trying next driver",
                        self.name, driver, msg.splitlines()[0][:200],
                    )
                    continue
                raise UserError(_("Connection failed: %s") % exc)
            if driver != candidates[0]:
                _logger.info(
                    "ESSL %s: connected via fallback driver %r", self.name, driver,
                )
            conn.timeout = timeout
            return conn

        raise UserError(_(
            "Connection failed for all installed drivers (%s). Last error: %s"
        ) % (", ".join(candidates), last_exc))

    def action_test_connection(self):
        self.ensure_one()
        self._validate_identifier(self.devices_table, "Devices Table")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM [%s]" % self.devices_table)
            row = cur.fetchone()
            cnt = int(row[0]) if row else 0
            cur.close()
        except Exception as exc:
            raise UserError(_("Query failed: %s") % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ESSL Server"),
                "message": _("Connection OK. %d device(s) in the source table.") % cnt,
                "sticky": False,
                "type": "success",
            },
        }

    def action_pull_devices_now(self):
        self.ensure_one()
        res = self._pull_devices()
        msg = _(
            "Devices synced. Created: %(created)d, Updated: %(updated)d, "
            "Unchanged: %(unchanged)d, Failed: %(failed)d."
        ) % res
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ESSL Device Sync"),
                "message": msg,
                "sticky": False,
                "type": "success",
            },
        }

    def action_pull_all_attendance(self):
        self.ensure_one()
        started = time.monotonic()
        now_ist = datetime.now(_IST).replace(tzinfo=None)
        today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        date_from = today_start_ist.replace(day=1) - timedelta(days=365)
        date_from = date_from.replace(day=1)
        date_to = today_start_ist + timedelta(days=1)

        totals = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        batches = 0
        truncated = False
        cursor = date_from
        while cursor < date_to:
            if int((time.monotonic() - started) * 1000) > _ATTENDANCE_TIME_BUDGET_MS:
                truncated = True
                break
            if cursor.month == 12:
                batch_end = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                batch_end = cursor.replace(month=cursor.month + 1, day=1)
            if batch_end > date_to:
                batch_end = date_to
            stats = self._pull_hr_attendance(date_from=cursor, date_to=batch_end)
            self.env.cr.commit()
            totals["created"] += stats["created"]
            totals["updated"] += stats["updated"]
            totals["skipped"] += stats["skipped"]
            totals["errors"] += len(stats["errors"])
            batches += 1
            cursor = batch_end

        parts = [
            _("Attendance sync %(from)s → %(to)s.") % {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": (date_to - timedelta(days=1)).strftime("%Y-%m-%d"),
            },
            _("Created: %(c)d, Updated: %(u)d, Skipped: %(s)d, Errors: %(e)d, Batches: %(b)d.") % {
                "c": totals["created"], "u": totals["updated"],
                "s": totals["skipped"], "e": totals["errors"], "b": batches,
            },
        ]
        if truncated:
            parts.append(
                _("Time budget hit at %(nf)s — click again to continue.") % {
                    "nf": cursor.strftime("%Y-%m-%d"),
                }
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ESSL Attendance Sync"),
                "message": " ".join(parts),
                "sticky": bool(truncated or totals["errors"]),
                "type": "warning" if truncated or totals["errors"] else "success",
            },
        }

    def _pull_devices(self):
        self.ensure_one()
        self._validate_identifier(self.devices_table, "Devices Table")
        try:
            conn = self._connect()
        except UserError as exc:
            self.sudo().write({"last_device_sync_error": str(exc)[:1000]})
            raise
        try:
            cur = conn.cursor()
            sql = "SELECT %s FROM [%s]" % (
                ", ".join("[%s]" % c for c in DEVICE_SOURCE_COLUMNS),
                self.devices_table,
            )
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
        except Exception as exc:
            self.sudo().write({"last_device_sync_error": str(exc)[:1000]})
            raise UserError(_("Devices query failed: %s") % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        Device = self.env["essl.device"].sudo()
        existing = {
            d.external_device_id: d
            for d in Device.search([("server_id", "=", self.id)])
        }
        created = updated = unchanged = failed = 0
        stale_cutoff = fields.Datetime.now() - timedelta(days=365)

        for row in rows:
            ext_raw = row.get("DeviceId")
            if ext_raw is None:
                continue
            try:
                ext_id = int(ext_raw)
            except (TypeError, ValueError):
                continue

            vals = self._map_source_row(row, ext_id)

            try:
                rec = existing.get(ext_id)
                if rec:
                    changes = {k: v for k, v in vals.items() if rec[k] != v}
                    if changes:
                        rec.write(changes)
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    vals["sync_enabled"] = self._default_sync_enabled(vals, stale_cutoff)
                    Device.create(vals)
                    created += 1
            except Exception as exc:
                failed += 1
                _logger.warning(
                    "ESSL device upsert failed for DeviceId %s on server %s: %s",
                    ext_id, self.name, exc,
                )

        self.sudo().write({
            "last_device_sync_at": fields.Datetime.now(),
            "last_device_sync_count": created + updated,
            "last_device_sync_error": False,
        })
        _logger.info(
            "ESSL %s: pulled devices — created=%d updated=%d unchanged=%d failed=%d",
            self.name, created, updated, unchanged, failed,
        )
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
        }

    def _map_source_row(self, row, ext_id):
        def _s(key):
            v = row.get(key)
            if v is None:
                return False
            v = str(v).strip()
            return v or False

        def _ts(key):
            v = row.get(key)
            if not v:
                return False
            if hasattr(v, "year") and v.year < 1990:
                return False
            return v

        return {
            "server_id": self.id,
            "external_device_id": ext_id,
            "name": _s("DeviceFName") or ("Device %s" % ext_id),
            "short_name": _s("DeviceSName"),
            "direction_mode": _s("DeviceDirection"),
            "serial_number": _s("SerialNumber"),
            "connection_type": _s("ConnectionType"),
            "ip_address": _s("IpAddress"),
            "last_ping_at": _ts("LastPing"),
            "last_log_download_at": _ts("LastLogDownloadDate"),
            "device_type": _s("DeviceType"),
            "device_location": _s("DeviceLocation"),
            "timezone_minutes": int(row.get("Timezone") or 0),
            "face_device_type": _s("FaceDeviceType"),
            "activation_code": _s("DeviceActivationCode"),
        }

    @staticmethod
    def _default_sync_enabled(vals, stale_cutoff):
        if (vals.get("device_type") or "").lower() == "canteen":
            return False
        if not vals.get("serial_number"):
            return False
        last_ping = vals.get("last_ping_at")
        if last_ping and stale_cutoff and last_ping < stale_cutoff:
            return False
        return True

    def _pull_attendance_logs(self, date_from_ist, date_to_ist):
        self.ensure_one()
        self._validate_identifier(self.attendance_logs_table, "Attendance Logs Table")
        self._validate_identifier(self.employees_table, "Employees Table")

        try:
            conn = self._connect()
        except UserError:
            raise

        try:
            cur = conn.cursor()
            sql = (
                "SELECT a.[AttendanceLogId], a.[AttendanceDate], a.[EmployeeId], "
                "       a.[InTime], a.[InDeviceId], a.[OutTime], a.[OutDeviceId], "
                "       a.[Duration], a.[Present], a.[Absent], a.[IsOnLeave], "
                "       a.[LeaveType], a.[LeaveTypeId], a.[LeaveDuration], a.[LeaveRemarks], "
                "       a.[WeeklyOff], a.[Holiday], a.[OverTime], a.[Status], a.[StatusCode], "
                "       a.[PunchRecords], a.[Remarks], "
                "       e.[EmployeeCode] "
                "FROM [%s] a "
                "LEFT JOIN [%s] e ON e.[EmployeeId] = a.[EmployeeId] "
                "WHERE a.[AttendanceDate] >= ? AND a.[AttendanceDate] < ? "
                "ORDER BY a.[AttendanceDate], a.[EmployeeId]"
            ) % (self.attendance_logs_table, self.employees_table)
            cur.execute(sql, (date_from_ist, date_to_ist))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
        except Exception as exc:
            raise UserError(_("AttendanceLogs query failed: %s") % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        Daily = self.env["essl.attendance.daily"].sudo()
        code_map = Daily._build_code_to_employee_map()

        existing = Daily.search([
            ("server_id", "=", self.id),
            ("source_attendance_log_id", "in", [r["AttendanceLogId"] for r in rows]),
        ])
        existing_by_source = {r.source_attendance_log_id: r for r in existing}

        stats = {"total": len(rows), "created": 0, "updated": 0, "unchanged": 0,
                 "unmatched": 0, "failed": 0}
        touched_records = Daily.browse()

        for row in rows:
            try:
                source_id = int(row["AttendanceLogId"])
                emp_code = (row.get("EmployeeCode") or "").strip()
                in_time_src = row.get("InTime")
                out_time_src = row.get("OutTime")
                in_time_dt = self._coerce_datetime(in_time_src)
                out_time_dt = self._coerce_datetime(out_time_src)
                if in_time_dt and in_time_dt.year < 2000:
                    in_time_dt = False
                if out_time_dt and out_time_dt.year < 2000:
                    out_time_dt = False
                att_date = row.get("AttendanceDate")
                if hasattr(att_date, "date"):
                    att_date = att_date.date()

                vals = {
                    "server_id": self.id,
                    "source_attendance_log_id": source_id,
                    "attendance_date": att_date,
                    "source_employee_id": int(row["EmployeeId"]) if row.get("EmployeeId") is not None else False,
                    "employee_code": emp_code or False,
                    "employee_id": code_map.get(emp_code) or False,
                    "in_time": _ist_to_utc(in_time_dt) if in_time_dt else False,
                    "out_time": _ist_to_utc(out_time_dt) if out_time_dt else False,
                    "in_device_short": (row.get("InDeviceId") or "").strip() or False,
                    "out_device_short": (row.get("OutDeviceId") or "").strip() or False,
                    "duration_minutes": float(row.get("Duration") or 0.0),
                    "present": float(row.get("Present") or 0.0),
                    "absent": float(row.get("Absent") or 0.0),
                    "is_on_leave": bool(row.get("IsOnLeave")),
                    "leave_type": (row.get("LeaveType") or "").strip() or False,
                    "leave_type_id": int(row.get("LeaveTypeId") or 0) or False,
                    "leave_duration": float(row.get("LeaveDuration") or 0.0),
                    "leave_remarks": (row.get("LeaveRemarks") or "").strip() or False,
                    "weekly_off": bool(row.get("WeeklyOff")),
                    "holiday": bool(row.get("Holiday")),
                    "over_time": float(row.get("OverTime") or 0.0),
                    "status": (row.get("Status") or "").strip() or False,
                    "status_code": (row.get("StatusCode") or "").strip() or False,
                    "punch_records": row.get("PunchRecords") or False,
                    "remarks": (row.get("Remarks") or "").strip() or False,
                    "error_note": False if code_map.get(emp_code) else (
                        "No hr.employee for EmployeeCode=%s" % (emp_code or "(blank)")),
                }

                rec = existing_by_source.get(source_id)
                if rec:
                    changed = {k: v for k, v in vals.items() if rec[k] != v}
                    if changed:
                        rec.write(changed)
                        stats["updated"] += 1
                    else:
                        stats["unchanged"] += 1
                    touched_records |= rec
                else:
                    new_rec = Daily.create(vals)
                    stats["created"] += 1
                    touched_records |= new_rec

                if not code_map.get(emp_code):
                    stats["unmatched"] += 1
            except Exception as exc:
                stats["failed"] += 1
                _logger.exception("essl.attendance.daily upsert failed for source id=%s: %s",
                                  row.get("AttendanceLogId"), exc)

        sync_stats = touched_records._sync_hr_attendance()
        stats["hr_attendance"] = sync_stats
        return stats

    @staticmethod
    def _coerce_datetime(value):
        if value is None or value == "":
            return None
        if hasattr(value, "year"):
            return value
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                from datetime import datetime as _dt
                return _dt.strptime(str(value), fmt)
            except (TypeError, ValueError):
                continue
        return None

    @api.model
    def _cron_pull_devices_all_servers(self):
        for srv in self.search([("active", "=", True)]):
            try:
                srv._pull_devices()
            except Exception as exc:
                srv.sudo().write({"last_device_sync_error": str(exc)[:1000]})
                _logger.exception("ESSL: pull-devices cron failed for %s", srv.name)

    def _pull_hr_attendance(self, date_from=None, date_to=None):
        self.ensure_one()
        from .essl_device import _ist_to_utc  # noqa: PLC0415

        conn = self._connect()
        try:
            cur = conn.cursor()
            sql = (
                "SELECT a.[AttendanceLogId], a.[AttendanceDate], e.[EmployeeCode], "
                "a.[InTime], a.[InDeviceId], a.[OutTime], a.[OutDeviceId], a.[Status], "
                "a.[Present], a.[Absent] "
                "FROM [AttendanceLogs] a "
                "INNER JOIN [Employees] e ON e.[EmployeeId] = a.[EmployeeId] "
                "WHERE 1=1"
            )
            params = []
            if date_from is not None:
                sql += " AND a.[AttendanceDate] >= ?"
                params.append(date_from.date() if hasattr(date_from, "date") else date_from)
            if date_to is not None:
                sql += " AND a.[AttendanceDate] < ?"
                params.append(date_to.date() if hasattr(date_to, "date") else date_to)
            sql += " ORDER BY a.[AttendanceDate], e.[EmployeeCode]"
            cur.execute(sql, tuple(params))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        Employee = self.env["hr.employee"].sudo()
        emp_map = {}
        for r in Employee.search_read([("employee_code", "!=", False)], ["id", "employee_code"]):
            code = (r["employee_code"] or "").strip()
            if code:
                emp_map[code] = r["id"]

        Device = self.env["essl.device"].sudo()
        device_location_map = {}
        for d in Device.search_read(
            [("server_id", "=", self.id)],
            ["short_name", "name", "location", "device_location"],
        ):
            key = (d.get("short_name") or "").strip()
            if not key:
                continue
            loc = (
                (d.get("location") or "").strip()
                or (d.get("device_location") or "").strip()
                or (d.get("name") or "").strip()
                or key
            )
            device_location_map[key] = loc

        Attendance = self.env["hr.attendance"].sudo()
        stats = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

        for row in rows:
            emp_code = (row.get("EmployeeCode") or "").strip()
            emp_id = emp_map.get(emp_code)
            if not emp_id:
                stats["skipped"] += 1
                continue
            in_time_raw = (row.get("InTime") or "").strip()
            out_time_raw = (row.get("OutTime") or "").strip()
            att_date_source = row.get("AttendanceDate")
            att_date_only = att_date_source.date() if hasattr(att_date_source, "date") else att_date_source
            has_real_intime = bool(in_time_raw) and not in_time_raw.startswith("1900-01-01")
            check_out_ist = None
            if has_real_intime:
                try:
                    check_in_ist = datetime.strptime(in_time_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    stats["errors"].append({"employee_code": emp_code, "error": "bad InTime %r" % in_time_raw})
                    continue
                if out_time_raw and not out_time_raw.startswith("1900-01-01"):
                    try:
                        check_out_ist = datetime.strptime(out_time_raw, "%Y-%m-%d %H:%M:%S")
                        if check_out_ist <= check_in_ist:
                            check_out_ist = None
                    except ValueError:
                        check_out_ist = None
                attendance_status = "present"
            else:
                if att_date_only is None:
                    stats["skipped"] += 1
                    continue
                check_in_ist = datetime.combine(att_date_only, datetime.min.time()).replace(hour=9)
                attendance_status = "absent"

            check_in_utc = _ist_to_utc(check_in_ist)
            check_out_utc = _ist_to_utc(check_out_ist) if check_out_ist else False

            in_dev_code = (row.get("InDeviceId") or "").strip()
            out_dev_code = (row.get("OutDeviceId") or "").strip()
            in_location = device_location_map.get(in_dev_code, in_dev_code) if in_dev_code else False
            out_location = device_location_map.get(out_dev_code, out_dev_code) if out_dev_code else False
            current_location = out_location if check_out_utc else (in_location or out_location)

            att_date = row.get("AttendanceDate")
            if hasattr(att_date, "date"):
                att_date = att_date.date()
            day_start_ist = datetime.combine(att_date, datetime.min.time())
            day_end_ist = day_start_ist + timedelta(days=1)
            day_start = _ist_to_utc(day_start_ist)
            day_end = _ist_to_utc(day_end_ist)

            try:
                with self.env.cr.savepoint():
                    existing = Attendance.search(
                        [
                            ("employee_id", "=", emp_id),
                            ("check_in", ">=", day_start),
                            ("check_in", "<", day_end),
                        ],
                        limit=1,
                        order="check_in asc",
                    )
                    if existing:
                        vals = {}
                        if existing.check_in != check_in_utc:
                            vals["check_in"] = check_in_utc
                        target_check_out = check_out_utc or False
                        if (existing.check_out or False) != target_check_out:
                            vals["check_out"] = target_check_out
                        target_in_loc = in_location or False
                        if (existing.in_location or False) != target_in_loc:
                            vals["in_location"] = target_in_loc
                        target_out_loc = out_location or False
                        if (existing.out_location or False) != target_out_loc:
                            vals["out_location"] = target_out_loc
                        target_geo_loc = current_location or False
                        if (existing.geo_location or False) != target_geo_loc:
                            vals["geo_location"] = target_geo_loc
                        if (existing.attendance_status or "") != attendance_status:
                            vals["attendance_status"] = attendance_status
                        if vals:
                            self.env.cr.execute(
                                "UPDATE hr_attendance SET "
                                "check_in = %s, check_out = %s, "
                                "in_location = %s, out_location = %s, "
                                "geo_location = %s, attendance_status = %s "
                                "WHERE id = %s",
                                (
                                    vals.get("check_in", existing.check_in),
                                    vals.get("check_out", existing.check_out),
                                    vals.get("in_location", existing.in_location),
                                    vals.get("out_location", existing.out_location),
                                    vals.get("geo_location", existing.geo_location),
                                    vals.get("attendance_status", existing.attendance_status),
                                    existing.id,
                                ),
                            )
                            existing.invalidate_recordset(
                                ["check_in", "check_out", "in_location",
                                 "out_location", "geo_location",
                                 "attendance_status", "worked_hours"]
                            )
                            self.env.add_to_compute(
                                Attendance._fields["worked_hours"], existing
                            )
                            stats["updated"] += 1
                        else:
                            stats["skipped"] += 1
                    else:
                        create_vals = {
                            "employee_id": emp_id,
                            "check_in": check_in_utc,
                            "attendance_status": attendance_status,
                        }
                        if check_out_utc:
                            create_vals["check_out"] = check_out_utc
                        if in_location:
                            create_vals["in_location"] = in_location
                        if out_location:
                            create_vals["out_location"] = out_location
                        if current_location:
                            create_vals["geo_location"] = current_location
                        try:
                            with self.env.cr.savepoint():
                                Attendance.create(create_vals)
                                self.env.flush_all()
                        except Exception as _create_exc:
                            _logger.warning(
                                "hr.attendance create fallback for emp=%s date=%s: %s: %s",
                                emp_code, att_date, type(_create_exc).__name__, str(_create_exc)[:200],
                            )
                            self.env.cr.execute(
                                "INSERT INTO hr_attendance "
                                "(employee_id, check_in, check_out, in_location, out_location, "
                                "geo_location, attendance_status, date, "
                                "create_uid, create_date, write_uid, write_date) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, "
                                "(%s AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::date, "
                                "1, NOW() AT TIME ZONE 'UTC', 1, NOW() AT TIME ZONE 'UTC') "
                                "RETURNING id",
                                (
                                    emp_id, check_in_utc, check_out_utc or None,
                                    in_location or None, out_location or None,
                                    current_location or None,
                                    attendance_status,
                                    check_in_utc,
                                ),
                            )
                            new_id = self.env.cr.fetchone()[0]
                            new_att = Attendance.browse(new_id)
                            new_att.invalidate_recordset(
                                ["check_in", "check_out", "worked_hours"]
                            )
                            self.env.add_to_compute(
                                Attendance._fields["worked_hours"], new_att
                            )
                        stats["created"] += 1
            except Exception as exc:
                stats["errors"].append({
                    "employee_code": emp_code,
                    "attendance_date": str(att_date),
                    "error": str(exc)[:300],
                })
        return stats
