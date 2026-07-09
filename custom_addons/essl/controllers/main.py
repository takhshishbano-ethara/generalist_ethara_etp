import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_API_TOKEN_PARAM = "essl_pull_api.token"
_TIME_BUDGET_MS = 110_000
_IST = ZoneInfo("Asia/Kolkata")


def _ist_to_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=_IST).astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_ist(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(_IST).replace(tzinfo=None)
    return dt.replace(tzinfo=timezone.utc).astimezone(_IST).replace(tzinfo=None)


class EsslPullApiController(http.Controller):

    @http.route(
        "/essl/api/pull",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def pull_all_devices(self, token=None, since=None, process=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response({"status": "error", "message": "Invalid or missing token"}, 401)

        started_at = time.monotonic()
        process_logs = process != "skip"
        try:
            since_override = self._parse_since(since)
            Device = request.env["essl.device"].sudo()
            Log = request.env["essl.attendance.log"].sudo()
            devices = Device.search([
                ("active", "=", True),
                ("sync_enabled", "=", True),
                ("server_id.active", "=", True),
            ])

            empty_device_set = Device.browse()
            devices_by_server = {}
            for device in devices:
                devices_by_server.setdefault(device.server_id, empty_device_set)
                devices_by_server[device.server_id] |= device

            per_device = []
            totals = {"created": 0, "processed": 0, "failed": 0}
            employee_map = Log.build_employee_map()
        except Exception as exc:
            _logger.exception("ESSL pull API: prep phase failed")
            return self._json_response(
                {"status": "error", "message": "prep failed: %s" % str(exc)[:500]},
                500,
            )

        truncated = False
        skipped = []
        for server, server_devices in devices_by_server.items():
            if self._budget_exceeded(started_at):
                truncated = True
                for device in server_devices:
                    skipped.append({"device_id": device.id, "device_name": device.display_name})
                continue

            conn = None
            try:
                conn = server.sudo()._connect()
            except Exception as exc:
                _logger.exception("ESSL pull API: server %s connect failed", server.name)
                for device in server_devices:
                    per_device.append({
                        "device_id": device.id,
                        "device_name": device.display_name,
                        "status": "error",
                        "message": "server connect failed: %s" % str(exc)[:300],
                    })
                continue

            try:
                for device in server_devices:
                    if self._budget_exceeded(started_at):
                        truncated = True
                        skipped.append({"device_id": device.id, "device_name": device.display_name})
                        continue
                    entry = {"device_id": device.id, "device_name": device.display_name}
                    try:
                        result = device._pull_attendance(
                            conn=conn,
                            employee_map=employee_map,
                            since_override=since_override,
                            process_logs=process_logs,
                        )
                        request.env.cr.commit()
                        entry.update({"status": "ok", **result})
                        for key in totals:
                            totals[key] += int(result.get(key, 0))
                    except Exception as exc:
                        request.env.cr.rollback()
                        _logger.exception("ESSL pull API: device %s failed", device.display_name)
                        entry.update({"status": "error", "message": str(exc)[:500]})
                    per_device.append(entry)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        response = {
            "status": "ok",
            "since": since_override.strftime("%Y-%m-%d %H:%M:%S") if since_override else "full",
            "process": "now" if process_logs else "skip",
            "devices_attempted": len(devices),
            "servers_used": len(devices_by_server),
            "totals": totals,
            "devices": per_device,
            "truncated": truncated,
            "skipped": skipped,
        }

        try:
            date_from, date_to = self._parse_date_range(
                kwargs.get("from"), kwargs.get("to")
            )
            response.update(self._build_attendance_payload(date_from, date_to))
        except Exception as exc:
            _logger.exception("ESSL pull API: attendance payload failed")
            response.update({
                "range": {"from": "", "to": "", "error": str(exc)[:300]},
                "employees": [],
                "attendances": [],
                "punches": [],
            })
            date_from, date_to = None, None

        if date_from and date_to:
            now_ist = datetime.now(_IST).replace(tzinfo=None)
            today_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow_ist = today_ist + timedelta(days=1)
            att_totals = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
            att_per_server = []
            for srv in devices_by_server.keys():
                entry = {"server_id": srv.id, "server_name": srv.name}
                try:
                    stats = srv._pull_hr_attendance(
                        date_from=today_ist, date_to=tomorrow_ist,
                    )
                    request.env.cr.commit()
                    entry.update({
                        "status": "ok",
                        "created": stats["created"],
                        "updated": stats["updated"],
                        "skipped": stats["skipped"],
                        "errors": stats["errors"],
                    })
                    att_totals["created"] += stats["created"]
                    att_totals["updated"] += stats["updated"]
                    att_totals["skipped"] += stats["skipped"]
                    att_totals["errors"] += len(stats["errors"])
                except Exception as exc:
                    request.env.cr.rollback()
                    _logger.exception(
                        "ESSL pull API: AttendanceLogs sync failed for %s", srv.name,
                    )
                    entry.update({"status": "error", "message": str(exc)[:500]})
                att_per_server.append(entry)
            response["attendance_logs_sync"] = {
                "totals": att_totals, "servers": att_per_server,
            }

            try:
                reconcile_stats = self._reconcile_hr_attendance(date_from, date_to)
                request.env.cr.commit()
                response["attendance_reconcile"] = reconcile_stats
            except Exception as exc:
                request.env.cr.rollback()
                _logger.exception("ESSL pull API: hr.attendance reconcile failed")
                response["attendance_reconcile"] = {"error": str(exc)[:300]}

            try:
                response.update(self._build_daily_summary_payload(date_from, date_to))
            except Exception as exc:
                _logger.exception("ESSL pull API: daily summary failed")
                response["daily_summary"] = []
                response["daily_summary_error"] = str(exc)[:300]

        response["duration_ms"] = int((time.monotonic() - started_at) * 1000)
        return self._json_response(response)

    def _budget_exceeded(self, started_at):
        return int((time.monotonic() - started_at) * 1000) > _TIME_BUDGET_MS

    def _parse_date_range(self, from_str, to_str):
        now_ist = datetime.now(_IST).replace(tzinfo=None)
        today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

        def _parse(value, default):
            if not value:
                return default
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except (TypeError, ValueError):
                return default

        date_from_ist = _parse(from_str, today_start_ist)
        date_to_ist = _parse(to_str, today_start_ist) + timedelta(days=1)
        return _ist_to_utc(date_from_ist), _ist_to_utc(date_to_ist)

    def _format_dt(self, dt):
        if not dt:
            return ""
        return _utc_to_ist(dt).strftime("%Y-%m-%d %H:%M:%S")

    def _get_attendance_locations(self, attendance_ids):
        if not attendance_ids:
            return {}
        request.env.cr.execute(
            """
            SELECT DISTINCT ON (l.attendance_id)
                l.attendance_id,
                COALESCE(
                    NULLIF(TRIM(d.location), ''),
                    NULLIF(TRIM(d.device_location), ''),
                    d.name,
                    ''
                ) AS location
            FROM essl_attendance_log l
            LEFT JOIN essl_device d ON d.id = l.device_id
            WHERE l.attendance_id IN %s
            ORDER BY l.attendance_id, l.punch_timestamp ASC
            """,
            (tuple(attendance_ids),),
        )
        return {row[0]: row[1] or "" for row in request.env.cr.fetchall()}

    def _build_attendance_payload(self, date_from, date_to):
        env = request.env

        employees = env["hr.employee"].sudo().search([("active", "=", True)])
        employees_data = []
        for emp in employees:
            employees_data.append({
                "employee_id": emp.id,
                "name": emp.name or "",
                "email": emp.work_email or "",
                "employee_code": emp.employee_code or "",
                "department": emp.department_id.name if emp.department_id else "",
                "job_title": emp.job_title or "",
                "user_id": emp.user_id.id if emp.user_id else False,
                "user_login": emp.user_id.login if emp.user_id else "",
            })

        attendances = env["hr.attendance"].sudo().search(
            [("check_in", ">=", date_from), ("check_in", "<", date_to)],
            order="check_in asc",
        )
        locations = self._get_attendance_locations(attendances.ids)
        attendances_data = []
        for att in attendances:
            emp = att.employee_id
            attendances_data.append({
                "id": att.id,
                "employee_id": emp.id if emp else False,
                "employee_name": (emp.name or "") if emp else "",
                "employee_code": (emp.employee_code or "") if emp else "",
                "check_in": self._format_dt(att.check_in),
                "check_out": self._format_dt(att.check_out),
                "working_hours": round(att.worked_hours, 2) if att.worked_hours else 0.0,
                "location": locations.get(att.id, ""),
            })

        punches = env["essl.attendance.log"].sudo().search(
            [("punch_timestamp", ">=", date_from), ("punch_timestamp", "<", date_to)],
            order="punch_timestamp asc",
        )
        status_map = {
            "0": "Check In",
            "1": "Check Out",
            "4": "OT Check In",
            "5": "OT Check Out",
        }
        punches_data = []
        for p in punches:
            device = p.device_id
            device_location = ""
            if device:
                device_location = (
                    (device.location or "").strip()
                    or (device.device_location or "").strip()
                    or device.name
                    or ""
                )
            emp = p.employee_id
            punches_data.append({
                "id": p.id,
                "employee_id": emp.id if emp else False,
                "employee_name": (emp.name or "") if emp else "",
                "device_user_id": p.device_user_id or "",
                "punch_time": self._format_dt(p.punch_timestamp),
                "status": status_map.get(p.status, p.status or ""),
                "device_id": device.id if device else False,
                "device_name": (device.name or "") if device else "",
                "location": device_location,
            })

        return {
            "range": {
                "from": _utc_to_ist(date_from).strftime("%Y-%m-%d"),
                "to": (_utc_to_ist(date_to) - timedelta(days=1)).strftime("%Y-%m-%d"),
                "timezone": "IST (Asia/Kolkata)",
            },
            "employees": employees_data,
            "attendances": attendances_data,
            "punches": punches_data,
        }

    def _format_time_ampm(self, dt):
        if not dt:
            return ""
        ist = _utc_to_ist(dt)
        s = ist.strftime("%I:%M %p")
        if s.startswith("0"):
            s = s[1:]
        return s

    def _reconcile_hr_attendance(self, date_from, date_to):
        env = request.env
        env.cr.execute(
            """
            SELECT
                l.employee_id,
                ((l.punch_timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::date) AS punch_date_ist,
                MIN(l.punch_timestamp) AS first_ts,
                MAX(l.punch_timestamp) AS last_ts
            FROM essl_attendance_log l
            WHERE l.employee_id IS NOT NULL
              AND l.punch_timestamp >= %s
              AND l.punch_timestamp <  %s
            GROUP BY l.employee_id, ((l.punch_timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::date)
            """,
            (date_from, date_to),
        )
        rows = env.cr.fetchall()
        Attendance = env["hr.attendance"].sudo()
        stats = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for employee_id, punch_date_ist, first_ts, last_ts in rows:
            day_start_ist = datetime.combine(punch_date_ist, datetime.min.time())
            day_end_ist = day_start_ist + timedelta(days=1)
            day_start = _ist_to_utc(day_start_ist)
            day_end = _ist_to_utc(day_end_ist)
            new_check_out = last_ts if last_ts and last_ts != first_ts else False
            try:
                with env.cr.savepoint():
                    existing = Attendance.search(
                        [
                            ("employee_id", "=", employee_id),
                            ("check_in", ">=", day_start),
                            ("check_in", "<",  day_end),
                        ],
                        limit=1,
                        order="check_in asc",
                    )
                    if existing:
                        stats["unchanged"] += 1
                    else:
                        create_vals = {
                            "employee_id": employee_id,
                            "check_in": first_ts,
                        }
                        if new_check_out:
                            create_vals["check_out"] = new_check_out
                        Attendance.create(create_vals)
                        stats["created"] += 1
            except Exception:
                _logger.exception(
                    "ESSL reconcile: employee_id=%s date=%s failed",
                    employee_id, punch_date_ist,
                )
                stats["failed"] += 1
        return stats

    def _build_daily_summary_payload(self, date_from, date_to):
        env = request.env
        date_str = _utc_to_ist(date_from).strftime("%Y-%m-%d")

        env.cr.execute(
            """
            WITH punches AS (
                SELECT
                    l.id,
                    l.employee_id,
                    l.device_user_id,
                    l.punch_timestamp,
                    l.device_id,
                    d.name AS device_name,
                    COALESCE(
                        NULLIF(TRIM(d.location), ''),
                        NULLIF(TRIM(d.device_location), ''),
                        d.name,
                        ''
                    ) AS location
                FROM essl_attendance_log l
                LEFT JOIN essl_device d ON d.id = l.device_id
                WHERE l.punch_timestamp >= %s
                  AND l.punch_timestamp <  %s
            ),
            agg AS (
                SELECT
                    employee_id,
                    device_user_id,
                    MIN(punch_timestamp) AS first_ts,
                    MAX(punch_timestamp) AS last_ts,
                    COUNT(*)              AS total_punches
                FROM punches
                GROUP BY employee_id, device_user_id
            ),
            first_row AS (
                SELECT DISTINCT ON (employee_id, device_user_id)
                    employee_id,
                    device_user_id,
                    device_id   AS in_device_id,
                    device_name AS in_device_name,
                    location    AS in_location
                FROM punches
                ORDER BY employee_id, device_user_id, punch_timestamp ASC
            ),
            last_row AS (
                SELECT DISTINCT ON (employee_id, device_user_id)
                    employee_id,
                    device_user_id,
                    device_id   AS out_device_id,
                    device_name AS out_device_name,
                    location    AS out_location
                FROM punches
                ORDER BY employee_id, device_user_id, punch_timestamp DESC
            )
            SELECT
                a.employee_id, a.device_user_id, a.first_ts, a.last_ts, a.total_punches,
                fr.in_device_name, fr.in_location,
                lr.out_device_name, lr.out_location
            FROM agg a
            LEFT JOIN first_row fr
                ON (fr.employee_id IS NOT DISTINCT FROM a.employee_id)
               AND (fr.device_user_id = a.device_user_id)
            LEFT JOIN last_row lr
                ON (lr.employee_id IS NOT DISTINCT FROM a.employee_id)
               AND (lr.device_user_id = a.device_user_id)
            """,
            (date_from, date_to),
        )
        rows = env.cr.fetchall()

        def _make_entry(first_ts, last_ts, total_punches,
                        in_device, in_loc, out_device, out_loc):
            has_out = bool(last_ts and last_ts != first_ts)
            hours = 0.0
            if has_out:
                hours = round((last_ts - first_ts).total_seconds() / 3600.0, 2)
            return {
                "date": date_str,
                "check_in": self._format_time_ampm(first_ts),
                "check_out": self._format_time_ampm(last_ts) if has_out else "",
                "check_in_datetime": self._format_dt(first_ts),
                "check_out_datetime": self._format_dt(last_ts) if has_out else "",
                "location": in_loc or out_loc or "",
                "check_in_location": in_loc or "",
                "check_out_location": out_loc or "",
                "check_in_device": in_device or "",
                "check_out_device": out_device or "",
                "total_punches": int(total_punches or 0),
                "total_working_hours": hours,
            }

        by_employee = {}
        unmatched = []
        for row in rows:
            (emp_id, device_user_id, first_ts, last_ts, total_punches,
             in_device, in_loc, out_device, out_loc) = row
            entry = _make_entry(
                first_ts, last_ts, total_punches,
                in_device, in_loc, out_device, out_loc,
            )
            entry["device_user_id"] = device_user_id or ""
            if emp_id:
                by_employee[emp_id] = entry
            else:
                unmatched.append(entry)

        employees = env["hr.employee"].sudo().search(
            [("active", "=", True)], order="name asc"
        )
        summary = []
        for emp in employees:
            record = {
                "employee_id": emp.id,
                "employee_code": emp.employee_code or "",
                "employee_name": emp.name or "",
                "department": emp.department_id.name if emp.department_id else "",
                "date": date_str,
                "check_in": "",
                "check_out": "",
                "check_in_datetime": "",
                "check_out_datetime": "",
                "location": "",
                "check_in_location": "",
                "check_out_location": "",
                "check_in_device": "",
                "check_out_device": "",
                "device_user_id": "",
                "total_punches": 0,
                "total_working_hours": 0.0,
                "status": "absent",
            }
            punch = by_employee.get(emp.id)
            if punch:
                record.update(punch)
                record["status"] = "present"
            summary.append(record)

        return {
            "date": date_str,
            "daily_summary": summary,
            "unmatched_punches": unmatched,
        }

    def _parse_since(self, raw):
        now_ist = datetime.now(_IST).replace(tzinfo=None)
        if raw is None or raw == "" or raw == "today":
            return now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        if raw == "full":
            return None
        if isinstance(raw, str) and raw.endswith("h") and raw[:-1].isdigit():
            return now_ist - timedelta(hours=int(raw[:-1]))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except (TypeError, ValueError):
                continue
        return now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

    @http.route(
        "/essl/api/pull_all_attendance",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def pull_all_attendance(self, token=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response(
                {"status": "error", "message": "Invalid or missing token"}, 401
            )

        started_at = time.monotonic()
        now_ist = datetime.now(_IST).replace(tzinfo=None)
        today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

        date_from = None
        if kwargs.get("from"):
            try:
                date_from = datetime.strptime(kwargs["from"], "%Y-%m-%d")
            except (TypeError, ValueError):
                pass
        if date_from is None:
            date_from = today_start_ist.replace(day=1) - timedelta(days=365)
            date_from = date_from.replace(day=1)

        date_to = None
        if kwargs.get("to"):
            try:
                date_to = datetime.strptime(kwargs["to"], "%Y-%m-%d") + timedelta(days=1)
            except (TypeError, ValueError):
                pass
        if date_to is None:
            date_to = today_start_ist + timedelta(days=1)

        Server = request.env["essl.server"].sudo()
        servers = Server.search([("active", "=", True)])
        per_server = {srv.id: {
            "server_id": srv.id, "server_name": srv.name, "status": "ok",
            "created": 0, "updated": 0, "skipped": 0, "errors": [],
            "absent_created": 0, "absent_skipped": 0, "absent_unmapped": 0,
        } for srv in servers}
        totals = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        absent_totals = {"created": 0, "skipped": 0, "unmapped": 0, "errors": 0}

        truncated = False
        batches_processed = 0
        cursor = date_from
        while cursor < date_to:
            if int((time.monotonic() - started_at) * 1000) > _TIME_BUDGET_MS:
                truncated = True
                break
            if cursor.month == 12:
                batch_end = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                batch_end = cursor.replace(month=cursor.month + 1, day=1)
            if batch_end > date_to:
                batch_end = date_to

            for srv in servers:
                slot = per_server[srv.id]
                if slot["status"] != "ok":
                    continue
                try:
                    stats = srv._pull_hr_attendance(date_from=cursor, date_to=batch_end)
                    request.env.cr.commit()
                    slot["created"] += stats["created"]
                    slot["updated"] += stats["updated"]
                    slot["skipped"] += stats["skipped"]
                    slot["errors"].extend(stats["errors"])
                    totals["created"] += stats["created"]
                    totals["updated"] += stats["updated"]
                    totals["skipped"] += stats["skipped"]
                    totals["errors"] += len(stats["errors"])
                    absent = stats.get("absent_days") or {}
                    slot["absent_created"] += absent.get("created", 0)
                    slot["absent_skipped"] += absent.get("skipped", 0)
                    slot["absent_unmapped"] += absent.get("unmapped", 0)
                    absent_totals["created"] += absent.get("created", 0)
                    absent_totals["skipped"] += absent.get("skipped", 0)
                    absent_totals["unmapped"] += absent.get("unmapped", 0)
                    absent_totals["errors"] += len(absent.get("errors") or [])
                except Exception as exc:
                    request.env.cr.rollback()
                    _logger.exception(
                        "pull_all_attendance: server %s batch %s failed",
                        srv.name, cursor.strftime("%Y-%m"),
                    )
                    slot["status"] = "error"
                    slot["message"] = str(exc)[:500]
            batches_processed += 1
            cursor = batch_end

        return self._json_response({
            "status": "ok",
            "range": {
                "from": date_from.strftime("%Y-%m-%d"),
                "to": (date_to - timedelta(days=1)).strftime("%Y-%m-%d"),
            },
            "batches_processed": batches_processed,
            "truncated": truncated,
            "next_from": cursor.strftime("%Y-%m-%d") if truncated else None,
            "totals": totals,
            "absent_totals": absent_totals,
            "servers": list(per_server.values()),
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        })

    @http.route(
        "/essl/api/pull-devices",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def pull_devices(self, token=None, server_id=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response({"status": "error", "message": "Invalid or missing token"}, 401)

        started_at = time.monotonic()
        Server = request.env["essl.server"].sudo()
        if server_id:
            try:
                sid = int(server_id)
            except (TypeError, ValueError):
                return self._json_response(
                    {"status": "error", "message": "Invalid server_id"}, 400
                )
            servers = Server.search([("active", "=", True), ("id", "=", sid)])
        else:
            servers = Server.search([("active", "=", True)])

        per_server = []
        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for srv in servers:
            entry = {"server_id": srv.id, "server_name": srv.name}
            try:
                result = srv._pull_devices()
                request.env.cr.commit()
                entry.update({"status": "ok", **result})
                for key in totals:
                    totals[key] += int(result.get(key, 0))
            except Exception as exc:
                request.env.cr.rollback()
                _logger.exception("ESSL pull-devices API: server %s failed", srv.name)
                entry.update({"status": "error", "message": str(exc)[:500]})
            per_server.append(entry)

        return self._json_response({
            "status": "ok",
            "servers_attempted": len(servers),
            "totals": totals,
            "servers": per_server,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        })

    @http.route(
        "/essl/api/attendance/list",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def list_attendance(self, token=None, **kwargs):
        if not self._is_token_valid(token):
            return self._json_response(
                {"status": "error", "message": "Invalid or missing token"}, 401
            )

        try:
            page = max(1, int(kwargs.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = int(kwargs.get("limit") or 25)
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(200, limit))

        now_ist = datetime.now(_IST).replace(tzinfo=None)
        today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

        def _parse_day(value, default):
            if not value:
                return default
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except (TypeError, ValueError):
                return default

        from_ist = _parse_day(kwargs.get("from"), today_start_ist.replace(day=1))
        to_ist = _parse_day(kwargs.get("to"), today_start_ist) + timedelta(days=1)
        if to_ist <= from_ist:
            return self._json_response(
                {"status": "error", "message": "'to' must be on or after 'from'"},
                422,
            )
        date_from_utc = _ist_to_utc(from_ist)
        date_to_utc = _ist_to_utc(to_ist)

        domain = [
            ("check_in", ">=", date_from_utc),
            ("check_in", "<", date_to_utc),
        ]

        employee_id_raw = kwargs.get("employee_id") or kwargs.get("employeeId")
        if employee_id_raw:
            try:
                domain.append(("employee_id", "=", int(employee_id_raw)))
            except (TypeError, ValueError):
                pass

        department = (kwargs.get("department") or "").strip()
        if department:
            domain.append(("employee_id.department_id.name", "ilike", department))

        status_filter = (kwargs.get("status") or "").strip().lower()
        if status_filter:
            domain.append(("attendance_status", "=", status_filter))

        search = (kwargs.get("search") or "").strip()
        if search:
            domain += [
                "|", "|",
                ("employee_id.name", "ilike", search),
                ("employee_id.employee_code", "ilike", search),
                ("employee_id.department_id.name", "ilike", search),
            ]

        Attendance = request.env["hr.attendance"].sudo()
        total = Attendance.search_count(domain)
        records = Attendance.search(
            domain,
            order="check_in desc",
            offset=(page - 1) * limit,
            limit=limit,
        )

        data = []
        for att in records:
            emp = att.employee_id
            check_in_ist = _utc_to_ist(att.check_in) if att.check_in else None
            data.append({
                "id": att.id,
                "employeeId": emp.id if emp else False,
                "employeeName": (emp.name or "") if emp else "",
                "employeeCode": (emp.employee_code or "") if emp else "",
                "department": emp.department_id.name if emp and emp.department_id else "",
                "jobTitle": (emp.job_title or "") if emp else "",
                "attendanceDate": check_in_ist.strftime("%Y-%m-%d") if check_in_ist else "",
                "checkIn": self._format_dt(att.check_in),
                "checkOut": self._format_dt(att.check_out) if att.check_out else "",
                "workedHours": round(att.worked_hours, 2) if att.worked_hours else 0.0,
                "status": att.attendance_status or "",
                "checkInLocation": att.in_location or "",
                "checkOutLocation": att.out_location or "",
                "geoLocation": att.geo_location or "",
            })

        return self._json_response({
            "status": "ok",
            "data": data,
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": (total + limit - 1) // limit if limit else 1,
            "range": {
                "from": from_ist.strftime("%Y-%m-%d"),
                "to": (to_ist - timedelta(days=1)).strftime("%Y-%m-%d"),
                "timezone": "IST (Asia/Kolkata)",
            },
        })

    def _is_token_valid(self, supplied_token):
        if not supplied_token:
            auth_header = request.httprequest.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                supplied_token = auth_header[7:]
        if not supplied_token:
            return False
        configured = request.env["ir.config_parameter"].sudo().get_param(_API_TOKEN_PARAM, "")
        if not configured:
            return False
        return hmac.compare_digest(str(supplied_token), str(configured))

    def _json_response(self, payload, status=200):
        return request.make_response(
            json.dumps(payload, default=str),
            status=status,
            headers=[("Content-Type", "application/json")],
        )
