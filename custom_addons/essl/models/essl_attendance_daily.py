# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_SENTINEL_YEAR = 2000


def _ist_to_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=_IST).astimezone(timezone.utc).replace(tzinfo=None)


class EsslAttendanceDaily(models.Model):
    _name = "essl.attendance.daily"
    _description = "ESSL Daily Attendance Summary (per employee per day, from AttendanceLogs)"
    _order = "attendance_date desc, employee_id"
    _rec_name = "attendance_date"

    server_id = fields.Many2one(
        "essl.server", string="ESSL Server", required=True,
        ondelete="cascade", index=True,
    )
    source_attendance_log_id = fields.Integer(
        string="Source AttendanceLogId", required=True, index=True,
    )
    attendance_date = fields.Date(string="Date", required=True, index=True)
    source_employee_id = fields.Integer(string="Source EmployeeId", index=True)
    employee_code = fields.Char(string="Employee Code", index=True)
    employee_id = fields.Many2one("hr.employee", string="Employee", index=True)

    in_time = fields.Datetime(string="In Time")
    out_time = fields.Datetime(string="Out Time")
    in_device_short = fields.Char(string="In Device")
    out_device_short = fields.Char(string="Out Device")

    duration_minutes = fields.Float(string="Duration (min)", default=0.0)
    present = fields.Float(string="Present", default=0.0)
    absent = fields.Float(string="Absent", default=0.0)
    is_on_leave = fields.Boolean(default=False)
    leave_type = fields.Char()
    leave_type_id = fields.Integer()
    leave_duration = fields.Float(default=0.0)
    leave_remarks = fields.Char()
    weekly_off = fields.Boolean(default=False)
    holiday = fields.Boolean(default=False)
    over_time = fields.Float(default=0.0)
    status = fields.Char()
    status_code = fields.Char(index=True)
    punch_records = fields.Text()
    remarks = fields.Char()

    attendance_id = fields.Many2one(
        "hr.attendance", string="HR Attendance", ondelete="set null",
    )
    error_note = fields.Char()

    _unique_source = models.Constraint(
        "unique(server_id, source_attendance_log_id)",
        "Duplicate AttendanceLog row for this server.",
    )

    @api.model
    def _build_code_to_employee_map(self):
        rows = self.env["hr.employee"].sudo().search_read(
            [("employee_code", "!=", False)], ["id", "employee_code"],
        )
        return {(r["employee_code"] or "").strip(): r["id"] for r in rows if r.get("employee_code")}

    @api.model
    def _is_real_time(self, dt):
        if not dt:
            return False
        try:
            return dt.year >= _SENTINEL_YEAR
        except AttributeError:
            return False

    def _sync_hr_attendance(self):
        Attendance = self.env["hr.attendance"].sudo()
        stats = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
        for rec in self:
            try:
                with self.env.cr.savepoint():
                    if not rec.employee_id or not rec.in_time or rec.present <= 0:
                        stats["skipped"] += 1
                        continue
                    day_start_ist = datetime.combine(rec.attendance_date, datetime.min.time())
                    day_end_ist = day_start_ist + timedelta(days=1)
                    day_start = _ist_to_utc(day_start_ist)
                    day_end = _ist_to_utc(day_end_ist)
                    existing = Attendance.search([
                        ("employee_id", "=", rec.employee_id.id),
                        ("check_in", ">=", day_start),
                        ("check_in", "<", day_end),
                    ], limit=1, order="check_in asc")
                    check_out = rec.out_time if rec.out_time and rec.out_time > rec.in_time else False
                    if existing:
                        vals = {}
                        if existing.check_in != rec.in_time:
                            vals["check_in"] = rec.in_time
                        if check_out and existing.check_out != check_out:
                            vals["check_out"] = check_out
                        if vals:
                            existing.write(vals)
                            stats["updated"] += 1
                        else:
                            stats["unchanged"] += 1
                        rec.attendance_id = existing.id
                    else:
                        create_vals = {
                            "employee_id": rec.employee_id.id,
                            "check_in": rec.in_time,
                        }
                        if check_out:
                            create_vals["check_out"] = check_out
                        att = Attendance.create(create_vals)
                        rec.attendance_id = att.id
                        stats["created"] += 1
            except Exception as exc:
                stats["failed"] += 1
                rec.sudo().write({"error_note": str(exc)[:500]})
                _logger.exception("essl.attendance.daily sync failed for id=%s", rec.id)
        return stats
