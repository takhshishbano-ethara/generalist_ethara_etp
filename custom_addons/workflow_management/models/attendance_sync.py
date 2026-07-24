import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AttendanceSync(models.Model):
    _name = "workflow.attendance.sync"
    _description = "Attendance Synchronization Configuration"
    _order = "last_sync desc"

    name = fields.Char(string="Name", default="ESSL Attendance Sync")
    active = fields.Boolean(default=True)

    # Connection settings (stored as ir.config_parameter for security)
    essl_host = fields.Char(string="ESSL Host")
    essl_port = fields.Integer(string="ESSL Port", default=1433)
    essl_user = fields.Char(string="ESSL User")
    essl_password = fields.Char(string="ESSL Password")
    essl_database = fields.Char(string="ESSL Database", default="ESSL")

    last_sync = fields.Datetime(string="Last Sync Time", readonly=True)
    sync_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("running", "Running"),
            ("success", "Success"),
            ("error", "Error"),
        ],
        string="Sync Status",
        default="idle",
        readonly=True,
    )
    error_log = fields.Text(string="Error Log", readonly=True)
    records_synced = fields.Integer(string="Records Synced", readonly=True)

    def action_test_connection(self):
        """Test ESSL SQL Server connectivity."""
        self.ensure_one()
        try:
            import pytds
        except ImportError:
            raise UserError(
                "pytds library is not installed. "
                "Run: pip install python-tds"
            )
        try:
            conn = pytds.connect(
                server=self.essl_host,
                port=self.essl_port,
                user=self.essl_user,
                password=self.essl_password,
                database=self.essl_database,
                login_timeout=10,
            )
            conn.close()
            self.write({"sync_status": "idle", "error_log": ""})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Connection Successful",
                    "message": f"Connected to {self.essl_host}:{self.essl_port}/{self.essl_database}",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            self.write({"sync_status": "error", "error_log": str(e)})
            raise UserError(f"Connection failed: {e}")

    def action_sync_now(self):
        """Manually trigger attendance sync from ESSL."""
        self.ensure_one()
        self._sync_attendance()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync Complete",
                "message": f"Synced {self.records_synced} attendance records.",
                "type": "success",
                "sticky": False,
            },
        }

    def _sync_attendance(self):
        """Core sync logic: fetch attendance from ESSL and create hr.attendance records."""
        self.ensure_one()
        self.write({"sync_status": "running", "error_log": ""})

        try:
            import pytds
        except ImportError:
            self.write({
                "sync_status": "error",
                "error_log": "pytds not installed",
            })
            return

        count = 0
        try:
            conn = pytds.connect(
                server=self.essl_host,
                port=self.essl_port,
                user=self.essl_user,
                password=self.essl_password,
                database=self.essl_database,
                login_timeout=30,
            )
            cursor = conn.cursor()

            # Fetch recent attendance (last 2 days to catch late entries)
            cursor.execute("""
                SELECT
                    e.EmployeeCode,
                    a.AttendanceDate,
                    a.InTime,
                    a.OutTime,
                    a.Status
                FROM dbo.AttendanceLogs a
                LEFT JOIN dbo.Employees e ON a.EmployeeId = e.EmployeeId
                WHERE a.AttendanceDate >= DATEADD(day, -2, GETDATE())
                ORDER BY a.AttendanceDate DESC
            """)

            HrAttendance = self.env["hr.attendance"]
            HrEmployee = self.env["hr.employee"]

            for row in cursor.fetchall():
                emp_code, att_date, in_time, out_time, status = row
                if not emp_code or not in_time:
                    continue

                employee = HrEmployee.search(
                    [("barcode", "=", emp_code)], limit=1,
                )
                if not employee:
                    continue

                from datetime import datetime
                check_in = datetime.combine(att_date, in_time)
                check_out = (
                    datetime.combine(att_date, out_time) if out_time else False
                )

                # Skip if already synced (check_in is unique per employee per day)
                existing = HrAttendance.search([
                    ("employee_id", "=", employee.id),
                    ("check_in", "=", check_in),
                ], limit=1)

                if existing:
                    # Update check_out if it changed
                    if check_out and existing.check_out != check_out:
                        existing.write({"check_out": check_out})
                    continue

                HrAttendance.create({
                    "employee_id": employee.id,
                    "check_in": check_in,
                    "check_out": check_out,
                })
                count += 1

            cursor.close()
            conn.close()

            self.write({
                "last_sync": fields.Datetime.now(),
                "sync_status": "success",
                "records_synced": count,
                "error_log": "",
            })

            self.env["workflow.audit.log"].log_event(
                event="attendance_synced",
                category="attendance",
                details={"records_synced": count, "source": self.essl_host},
            )

        except Exception as e:
            _logger.exception("ESSL attendance sync failed")
            self.write({
                "sync_status": "error",
                "error_log": str(e),
            })

    @api.model
    def cron_sync_attendance(self):
        """Called by ir.cron to auto-sync attendance."""
        configs = self.search([("active", "=", True)], limit=1)
        for config in configs:
            config._sync_attendance()
