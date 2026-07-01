# -*- coding: utf-8 -*-
import logging

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def _ist_day_bounds_utc(utc_dt):
    ist = utc_dt.replace(tzinfo=timezone.utc).astimezone(_IST).replace(tzinfo=None)
    start_ist = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = start_ist + timedelta(days=1)
    start_utc = start_ist.replace(tzinfo=_IST).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_ist.replace(tzinfo=_IST).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc

PUNCH_STATUS = [
    ("0", "Check In"),
    ("1", "Check Out"),
    ("4", "OT Check In"),
    ("5", "OT Check Out"),
]


class EsslAttendanceLog(models.Model):
    _name = "essl.attendance.log"
    _description = "ESSL Attendance Log"
    _order = "punch_timestamp desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    device_id = fields.Many2one(
        "essl.device", string="Device", required=True, ondelete="restrict", index=True
    )
    device_user_id = fields.Char(string="Device User ID", required=True, index=True)
    employee_id = fields.Many2one("hr.employee", string="Employee", index=True)
    punch_timestamp = fields.Datetime(string="Punch Time", required=True, index=True)
    status = fields.Selection(PUNCH_STATUS, string="Punch Type", required=True)
    verify_method = fields.Char(string="Verify Method")
    attendance_id = fields.Many2one("hr.attendance", string="Attendance Record")
    error_note = fields.Text(string="Error Note")

    _unique_punch = models.Constraint(
        "unique(device_id, device_user_id, punch_timestamp)",
        "Duplicate punch record.",
    )

    def _get_hr_responsible_id(self):
        group = self.env.ref("hr.group_hr_manager", raise_if_not_found=False)
        if group and group.user_ids:
            return group.user_ids[0].id
        return self.env.ref("base.user_admin").id

    @api.model
    def build_employee_map(self):
        rows = self.env["hr.employee"].sudo().search_read(
            [("employee_code", "!=", False)],
            ["id", "employee_code"],
        )
        emp_map = {}
        for row in rows:
            code = (row.get("employee_code") or "").strip()
            if code:
                emp_map[code] = row["id"]
        return emp_map

    @api.model
    def process_log(self, log_id, employee_map=None):
        log = self.browse(log_id)
        if not log.exists():
            raise Exception("permanent failure: log record does not exist")

        employee = self.env["hr.employee"]
        device_user_id = (log.device_user_id or "").strip()
        if employee_map is not None:
            emp_id = employee_map.get(device_user_id)
            if emp_id:
                employee = self.env["hr.employee"].browse(emp_id)
        if not employee and device_user_id:
            employee = self.env["hr.employee"].search([
                ("employee_code", "=", device_user_id),
            ], limit=1)
            if employee and employee_map is not None:
                employee_map[device_user_id] = employee.id
        if not employee:
            log.sudo().write({
                "error_note": "No employee mapped for device_user_id: %s" % log.device_user_id
            })
            log.sudo().activity_schedule(
                "mail.mail_activity_data_todo",
                summary="Unmapped biometric user: %s" % log.device_user_id,
                note="Device user ID <b>%s</b> has no employee mapping. "
                     "Set <i>Biometric Device ID</i> on the correct employee's HR Settings tab."
                     % log.device_user_id,
                user_id=log._get_hr_responsible_id(),
            )
            raise Exception(
                "permanent failure: no employee mapped for device_user_id %s"
                % log.device_user_id
            )

        log.sudo().write({"employee_id": employee.id})

        if log.status in ("0", "4"):
            day_start, day_end = _ist_day_bounds_utc(log.punch_timestamp)
            today_att = self.env["hr.attendance"].search(
                [
                    ("employee_id", "=", employee.id),
                    ("check_in", ">=", day_start),
                    ("check_in", "<", day_end),
                ],
                limit=1,
                order="check_in desc",
            )
            if today_att:
                gap = log.punch_timestamp - today_att.check_in
                if gap < timedelta(minutes=1):
                    log.sudo().write({
                        "error_note": "Duplicate scan within 1 min of attendance %d" % today_att.id,
                        "attendance_id": today_att.id,
                    })
                    return today_att.id
                if not today_att.check_out or log.punch_timestamp > today_att.check_out:
                    today_att.sudo().write({
                        "check_out": log.punch_timestamp,
                        "attendance_status": "present",
                    })
                log.sudo().write({"attendance_id": today_att.id, "error_note": False})
                return today_att.id
            att = self.env["hr.attendance"].sudo().create({
                "employee_id": employee.id,
                "check_in": log.punch_timestamp,
                "attendance_status": "present",
            })
            log.sudo().write({"attendance_id": att.id, "error_note": False})
            return att.id

        open_att = self.env["hr.attendance"].search(
            [("employee_id", "=", employee.id), ("check_out", "=", False)],
            limit=1,
            order="check_in desc",
        )
        if open_att:
            open_att.sudo().write({"check_out": log.punch_timestamp})
            log.sudo().write({"attendance_id": open_att.id, "error_note": False})
            return open_att.id

        day_start, day_end = _ist_day_bounds_utc(log.punch_timestamp)
        same_day_att = self.env["hr.attendance"].search(
            [
                ("employee_id", "=", employee.id),
                ("check_in", ">=", day_start),
                ("check_in", "<", day_end),
            ],
            limit=1,
            order="check_in desc",
        )
        if same_day_att:
            if not same_day_att.check_out or log.punch_timestamp > same_day_att.check_out:
                same_day_att.sudo().write({"check_out": log.punch_timestamp})
            log.sudo().write({"attendance_id": same_day_att.id, "error_note": False})
            return same_day_att.id

        log.sudo().write({
            "error_note": "Orphan check-out: no attendance for employee %d on %s" % (
                employee.id, log.punch_timestamp.date())
        })
        log.sudo().activity_schedule(
            "mail.mail_activity_data_todo",
            summary="Orphan check-out: %s" % employee.name,
            note="Check-out punch at <b>%s</b> arrived for <b>%s</b> "
                 "but no same-day attendance was found. "
                 "Please create the attendance entry manually."
                 % (log.punch_timestamp, employee.name),
            user_id=log._get_hr_responsible_id(),
        )
        raise Exception(
            "permanent failure: orphan check-out for employee %d" % employee.id
        )
