from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token
)
from datetime import datetime
import io
import json


class TaskForgeExportController(http.Controller):

    @http.route('/api/v2/taskforge/export/tasks', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_tasks(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            team_ids = employee._get_team_employee_ids()
            TaskLog = request.env['task.forge.log'].sudo()

            domain = [('employee_id', 'in', team_ids)]
            if kwargs.get('date_from'):
                domain.append(('date', '>=', kwargs['date_from']))
            if kwargs.get('date_to'):
                domain.append(('date', '<=', kwargs['date_to']))
            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs['project_id'])))

            tasks = TaskLog.search(domain, order='date desc, create_date desc')

            export_format = kwargs.get('format', 'csv')

            headers = [
                'Reference', 'Task Name', 'Tasker', 'Project', 'Date', 'Status',
                'Start Time', 'End Time', 'Time (mins)', 'Quality Score',
                'Blocker Reason', 'Start Screenshot', 'End Screenshot',
            ]

            rows = []
            for t in tasks:
                rows.append([
                    t.sequence or '',
                    t.name or '',
                    t.employee_id.name or '',
                    t.project_id.name if t.project_id else '',
                    str(t.date) if t.date else '',
                    t.state or '',
                    t.start_time.isoformat() if t.start_time else '',
                    t.end_time.isoformat() if t.end_time else '',
                    str(t.time_taken_mins or 0),
                    str(t.quality_score or 0),
                    t.blocker_reason or '',
                    t.start_screenshot_url or '',
                    t.end_screenshot_url or '',
                ])

            if export_format == 'xlsx':
                return self._export_xlsx(headers, rows, 'task_logs')
            else:
                return self._export_csv(headers, rows, 'task_logs')

        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/export/validated_bugs', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_validated_bugs(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Bug = request.env['task.forge.validated.bug'].sudo()
            bugs = Bug.search([], order='create_date desc')

            headers = [
                'Bug Title', 'Project', 'Reported By', 'QR', 'PL', 'Validated By',
                'Impact', 'Status', 'Description', 'Steps to Reproduce',
                'Pages Affected', 'Blocker Reason', 'QR Video', 'QR Image', 'Created',
            ]

            rows = []
            for b in bugs:
                rows.append([
                    b.name or '',
                    b.project_id.name if b.project_id else '',
                    b.employee_id.name if b.employee_id else '',
                    b.qr_id.name if b.qr_id else '',
                    b.pl_id.name if b.pl_id else '',
                    b.validated_by_id.name if b.validated_by_id else '',
                    b.impact or '',
                    b.state or '',
                    b.bug_description or '',
                    b.steps_to_reproduce or '',
                    b.pages_affected or '',
                    b.blocker_reason or '',
                    b.qr_video_url or '',
                    b.qr_image_url or '',
                    b.create_date.isoformat() if b.create_date else '',
                ])

            export_format = kwargs.get('format', 'csv')
            if export_format == 'xlsx':
                return self._export_xlsx(headers, rows, 'validated_bugs')
            else:
                return self._export_csv(headers, rows, 'validated_bugs')

        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _export_csv(self, headers, rows, filename):
        import csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        content = output.getvalue()

        return http.Response(
            content,
            status=200,
            headers={
                'Content-Type': 'text/csv',
                'Content-Disposition': f'attachment; filename="{filename}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"',
            }
        )

    def _export_xlsx(self, headers, rows, filename):
        try:
            import xlsxwriter
        except ImportError:
            try:
                import openpyxl
                return self._export_xlsx_openpyxl(headers, rows, filename)
            except ImportError:
                return return_Response(message="Neither xlsxwriter nor openpyxl installed", status=400)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})
        ws = wb.add_worksheet(filename)

        header_format = wb.add_format({'bold': True, 'bg_color': '#4472C4', 'font_color': 'white'})

        for col, header in enumerate(headers):
            ws.write(0, col, header, header_format)

        for row_idx, row in enumerate(rows, start=1):
            for col_idx, val in enumerate(row):
                ws.write(row_idx, col_idx, val)

        wb.close()
        content = output.getvalue()

        return http.Response(
            content,
            status=200,
            headers={
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Disposition': f'attachment; filename="{filename}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx"',
            }
        )

    def _export_xlsx_openpyxl(self, headers, rows, filename):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = filename
        ws.append(headers)
        for row in rows:
            ws.append(row)

        output = io.BytesIO()
        wb.save(output)
        content = output.getvalue()

        return http.Response(
            content,
            status=200,
            headers={
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Disposition': f'attachment; filename="{filename}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx"',
            }
        )
