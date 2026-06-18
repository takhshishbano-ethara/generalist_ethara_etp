import base64
import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta, time

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# IST is a fixed offset from UTC (no DST).
IST_OFFSET = timedelta(hours=5, minutes=30)

OFFICE_START = time(10, 0)        # 10:00 AM IST
GRACE_END = time(10, 30)          # 10:30 AM IST — late if first check-in is after this
DEFAULT_MAX_LATE_PER_MONTH = 3    # >3 lates in a calendar month => exceeded


class LateComingReport(models.AbstractModel):
    """Daily late-comer report + monthly late-limit alerts, emailed to HR-role users.

    Lateness is computed straight from hr.attendance.check_in (UTC), converted
    to IST. No existing model/field is modified. Each run also writes a CSV to
    custom_addons/late_coming_report/generated/ and attaches it to the email.
    """
    _name = 'late.coming.report'
    _description = 'Late Coming Report & HR Alerts'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _to_ist(self, dt_utc):
        """Convert a naive UTC datetime (as stored by Odoo) to naive IST."""
        return dt_utc + IST_OFFSET

    @api.model
    def _today_ist(self):
        return self._to_ist(fields.Datetime.now()).date()

    @api.model
    def _max_late_per_month(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'late_coming_report.max_late_per_month')
        try:
            return int(param) if param else DEFAULT_MAX_LATE_PER_MONTH
        except (TypeError, ValueError):
            return DEFAULT_MAX_LATE_PER_MONTH

    @api.model
    def _hr_recipient_emails(self):
        """Work emails of all active users whose role type (api.role.user_type) is 'hr'.

        Prefers the HR person's hr.employee.work_email (linked via user_id);
        falls back to the user's login/partner email only when no work email exists.
        """
        hr_users = self.env['res.users'].sudo().search([
            ('active', '=', True),
            ('user_role.user_type', '=', 'hr'),
        ])
        emails = []
        for user in hr_users:
            employee = self.env['hr.employee'].sudo().search(
                [('user_id', '=', user.id)], limit=1)
            work_email = (employee.work_email or '').strip() if employee else ''
            email = work_email or (user.email or '').strip() \
                or (user.partner_id.email or '').strip()
            if email and email not in emails:
                emails.append(email)
        return emails

    @api.model
    def _first_checkins_for_month(self, month_start_utc, day_end_utc):
        """Return {(employee_id, ist_date): {employee, check_in_ist}} for the window,
        keeping only the FIRST (earliest) check-in per employee per IST day."""
        attendances = self.env['hr.attendance'].sudo().search([
            ('employee_id.active', '=', True),
            ('check_in', '>=', fields.Datetime.to_string(month_start_utc)),
            ('check_in', '<', fields.Datetime.to_string(day_end_utc)),
            ('check_in', '!=', False),
        ], order='check_in asc')

        first = {}
        for att in attendances:
            ist = self._to_ist(att.check_in)
            key = (att.employee_id.id, ist.date())
            if key not in first:  # asc order => first seen is earliest
                first[key] = {'employee': att.employee_id, 'check_in_ist': ist}
        return first

    @api.model
    def _minutes_late(self, check_in_ist, report_date):
        """Minutes after 10:30 IST (0 if on time)."""
        grace_dt = datetime.combine(report_date, GRACE_END)
        if check_in_ist <= grace_dt:
            return 0
        return int((check_in_ist - grace_dt).total_seconds() // 60)

    @api.model
    def _collect(self, today):
        """For the IST calendar month up to `today`, return:
          - today_rows:  list of {employee, check_in_ist, is_late, minutes_late}
                         for EVERY employee with a first check-in today (late or not)
          - late_days:   {employee_id: number of late days this month}
          - employee_by_id: {employee_id: hr.employee}
        """
        month_start_ist = datetime(today.year, today.month, 1)
        month_start_utc = month_start_ist - IST_OFFSET
        day_end_ist = datetime(today.year, today.month, today.day) + timedelta(days=1)
        day_end_utc = day_end_ist - IST_OFFSET

        first = self._first_checkins_for_month(month_start_utc, day_end_utc)

        late_days = defaultdict(int)
        employee_by_id = {}
        today_rows = []

        for (emp_id, ist_date), info in first.items():
            ci = info['check_in_ist']
            is_late = ci.time() > GRACE_END
            if is_late:
                late_days[emp_id] += 1
            employee_by_id[emp_id] = info['employee']
            if ist_date == today:
                today_rows.append({
                    'employee': info['employee'],
                    'check_in_ist': ci,
                    'is_late': is_late,
                    'minutes_late': self._minutes_late(ci, today),
                })

        today_rows.sort(key=lambda r: r['check_in_ist'])
        return today_rows, late_days, employee_by_id

    # ------------------------------------------------------------------
    # Cron entry points
    # ------------------------------------------------------------------
    @api.model
    def _cron_daily_late_report(self):
        """Runs daily at 11:00 AM IST. Builds the day's late-comers CSV, uploads it
        to S3 (no local copy), and emails the late comers to HR."""
        today = self._today_ist()
        today_rows, late_days, _ = self._collect(today)
        max_late = self._max_late_per_month()

        # Late comers only (employees whose first check-in today was after 10:30 IST).
        late_rows = [r for r in today_rows if r['is_late']]

        # CSV (late comers only) -> uploaded to S3 (no local copy is kept).
        csv_bytes = self._daily_csv_bytes(today, late_rows, late_days)
        filename = 'late_comers_%s.csv' % today.isoformat()
        self._upload_csv_s3(filename, csv_bytes)  # saved to S3 only; link not emailed

        recipients = self._hr_recipient_emails()
        if not recipients:
            _logger.warning(
                'Late Coming Report: no HR-role recipients (api.role.user_type=hr). '
                'No email sent for %s.', today)
            return

        body = self._daily_report_body(today, late_rows, late_days, max_late)
        self._send_mail(
            recipients,
            'Daily Late-Coming Report — %s' % today.strftime('%d %b %Y'),
            body,
            attachments=[(filename, csv_bytes, 'text/csv')],
        )
        _logger.info(
            'Daily late report %s: %d checked in, %d late, sent to %s',
            today, len(today_rows), len(late_rows), ', '.join(recipients))

    @api.model
    def _cron_late_limit_alert(self):
        """Runs daily at 11:30 AM IST. Emails HR about employees who exceeded the
        monthly late limit (fires only for employees late again today)."""
        today = self._today_ist()
        today_rows, late_days, employee_by_id = self._collect(today)
        max_late = self._max_late_per_month()

        late_today_ids = {r['employee'].id for r in today_rows if r['is_late']}
        exceeded = {
            emp_id: late_days[emp_id]
            for emp_id in late_today_ids
            if late_days[emp_id] > max_late
        }
        if not exceeded:
            _logger.info('Late limit alert %s: nobody over limit today', today)
            return

        csv_bytes = self._exceeded_csv_bytes(today, exceeded, employee_by_id, max_late)
        filename = 'late_limit_exceeded_%s.csv' % today.isoformat()
        self._upload_csv_s3(filename, csv_bytes)  # saved to S3 only; link not emailed

        recipients = self._hr_recipient_emails()
        if not recipients:
            _logger.warning(
                'Late Coming Report: no HR-role recipients. No limit-alert email '
                'sent for %s.', today)
            return

        body = self._exceeded_body(today, exceeded, employee_by_id, max_late)
        self._send_mail(
            recipients,
            'Action Required: Late Arrival Limit Exceeded — %s'
            % today.strftime('%d %b %Y'),
            body,
            attachments=[(filename, csv_bytes, 'text/csv')],
        )
        _logger.info(
            'Late limit alert %s: %d over limit, sent to %s',
            today, len(exceeded), ', '.join(recipients))

    # ------------------------------------------------------------------
    # CSV builders
    # ------------------------------------------------------------------
    @api.model
    def _daily_csv_bytes(self, today, today_rows, late_days):
        """Columns: Employee Code, Employee Name, Work Email, Department, Date, Check-In Time, Late Minutes."""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            'Employee Code', 'Employee Name', 'Work Email', 'Department', 'Date',
            'Check-In Time (IST)', 'Late Minutes',
        ])
        for r in today_rows:
            emp = r['employee']
            writer.writerow([
                emp.employee_code or '',
                emp.name or '',
                emp.work_email or '',
                emp.department_id.name or '',
                today.isoformat(),
                r['check_in_ist'].strftime('%I:%M %p'),
                r['minutes_late'],
            ])
        return buf.getvalue().encode('utf-8')

    @api.model
    def _exceeded_csv_bytes(self, today, exceeded, employee_by_id, max_late):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            'Employee Code', 'Employee Name', 'Work Email', 'Department', 'Date',
            'Lates This Month', 'Allowed Limit', 'Status',
        ])
        for emp_id, count in sorted(exceeded.items(), key=lambda kv: -kv[1]):
            emp = employee_by_id.get(emp_id) or self.env['hr.employee'].browse(emp_id)
            writer.writerow([
                emp.employee_code or '', emp.name or '', emp.work_email or '',
                emp.department_id.name or '',
                today.isoformat(), count, max_late, 'Limit Exceeded',
            ])
        return buf.getvalue().encode('utf-8')

    # ------------------------------------------------------------------
    # Disk archive + email
    # ------------------------------------------------------------------
    @api.model
    def _upload_csv_s3(self, filename, content_bytes):
        """Upload the CSV to S3 and return its public URL (no local copy is kept).
        Returns False if no s3.connector is configured or the upload fails."""
        connector = self.env['s3.connector'].sudo().search([], limit=1)
        if not connector:
            _logger.warning(
                'Late Coming Report: no s3.connector configured — %s not uploaded.',
                filename)
            return False
        object_name = 'late_coming_reports/%s' % filename
        try:
            b64 = base64.b64encode(content_bytes).decode()
            url = connector.upload_base64(b64, object_name)
            _logger.info('Late Coming Report: uploaded %s -> %s', filename, url)
            return url
        except Exception as exc:  # noqa: BLE001 - never let S3 break the email
            _logger.warning(
                'Late Coming Report: S3 upload failed for %s (%s)', filename, exc)
            return False

    @api.model
    def _send_mail(self, recipients, subject, body_html, attachments=None):
        attachment_cmds = []
        for fname, content, mimetype in (attachments or []):
            att = self.env['ir.attachment'].sudo().create({
                'name': fname,
                'datas': base64.b64encode(content),
                'mimetype': mimetype,
                'res_model': self._name,
            })
            attachment_cmds.append((4, att.id))
        mail = self.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': body_html,
            'email_from': self.env.company.email or self.env.user.email_formatted,
            'email_to': ','.join(recipients),
            'attachment_ids': attachment_cmds,
            'auto_delete': False,
        })
        mail.send()

    # ------------------------------------------------------------------
    # Email bodies (HTML)
    # ------------------------------------------------------------------
    @api.model
    def _td(self, value, center=False, extra=''):
        align = 'text-align:center;' if center else ''
        return ('<td style="padding:6px 12px;border:1px solid #ddd;%s%s">%s</td>'
                % (align, extra, value))

    @api.model
    def _daily_report_body(self, today, late_rows, late_days, max_late):
        date_str = today.strftime('%A, %d %b %Y')
        if late_rows:
            cells = []
            for r in late_rows:
                emp = r['employee']
                count = late_days[emp.id]
                over = count > max_late
                count_cell = (
                    '<span style="color:#c0392b;font-weight:bold;">%d</span>' % count
                    if over else str(count))
                cells.append(
                    '<tr>'
                    + self._td(emp.name or '')
                    + self._td(emp.employee_code or '-')
                    + self._td(emp.work_email or '-')
                    + self._td(emp.department_id.name or '-')
                    + self._td(r['check_in_ist'].strftime('%I:%M %p') + ' IST', center=True)
                    + self._td('%d min' % r['minutes_late'], center=True)
                    + self._td(count_cell, center=True)
                    + '</tr>')
            table = (
                '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">'
                '<thead><tr style="background:#2c3e50;color:#fff;">'
                '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Employee</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;">Code</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Work Email</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Department</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;">Check-In (IST)</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;">Late By</th>'
                '<th style="padding:8px 12px;border:1px solid #ddd;">Lates This Month</th>'
                '</tr></thead><tbody>%s</tbody></table>' % ''.join(cells))
            intro = (
                '<p>Dear HR Team,</p>'
                '<p>Please find below the late-coming report for <b>%s</b>. '
                '<b>%d</b> employee(s) checked in after 10:30 AM IST (after the '
                'grace period). The same list is attached as a CSV file.</p>'
                % (date_str, len(late_rows)))
        else:
            table = ''
            intro = (
                '<p>Dear HR Team,</p>'
                '<p>No employees were late on <b>%s</b>. Everyone checked in by '
                '10:30 AM IST. &#10004;</p>' % date_str)

        return (
            '<div style="font-family:Arial,sans-serif;color:#333;font-size:13px;">'
            '<h2 style="color:#2c3e50;margin-bottom:8px;">Daily Late-Coming Report</h2>'
            '%s%s'
            '<p style="margin-top:18px;">Regards,<br/><b>Ethara HR Automation</b></p>'
            '</div>' % (intro, table))

    @api.model
    def _exceeded_body(self, today, exceeded, employee_by_id, max_late):
        rows = []
        for emp_id, count in sorted(exceeded.items(), key=lambda kv: -kv[1]):
            emp = employee_by_id.get(emp_id) or self.env['hr.employee'].browse(emp_id)
            rows.append(
                '<tr>'
                + self._td(emp.name or '')
                + self._td(emp.employee_code or '-')
                + self._td(emp.work_email or '-')
                + self._td(emp.department_id.name or '-')
                + self._td('%d' % count, center=True,
                           extra='color:#c0392b;font-weight:bold;')
                + self._td('%d' % max_late, center=True)
                + '</tr>')
        table = (
            '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">'
            '<thead><tr style="background:#c0392b;color:#fff;">'
            '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Employee</th>'
            '<th style="padding:8px 12px;border:1px solid #ddd;">Code</th>'
            '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Work Email</th>'
            '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;">Department</th>'
            '<th style="padding:8px 12px;border:1px solid #ddd;">Lates This Month</th>'
            '<th style="padding:8px 12px;border:1px solid #ddd;">Allowed</th>'
            '</tr></thead><tbody>%s</tbody></table>' % ''.join(rows))

        return (
            '<div style="font-family:Arial,sans-serif;color:#333;font-size:13px;">'
            '<h2 style="color:#c0392b;margin-bottom:8px;">Late Arrival Limit Exceeded</h2>'
            '<p>Dear HR Team,</p>'
            '<p>The following employee(s) have exceeded the monthly limit of '
            '<b>%d</b> late arrivals for <b>%s</b> and may require your attention. '
            'Details are attached as a CSV file.</p>%s'
            '<p style="margin-top:18px;">Regards,<br/><b>Ethara HR Automation</b></p>'
            '</div>' % (max_late, today.strftime('%B %Y'), table))
