import io
import base64
import logging
from datetime import datetime

from odoo import http, fields
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, generate_s3_link,
)

_logger = logging.getLogger(__name__)

# ── Brand Colors ──────────────────────────────────────────────────────────────
PRIMARY = '#1B2A4A'
ACCENT = '#2E86DE'
LIGHT_BG = '#F0F4F8'
WHITE = '#FFFFFF'
BORDER_COLOR = '#CBD5E1'
SUCCESS = '#27AE60'
DANGER = '#E74C3C'
WARNING = '#F39C12'
TEXT_DARK = '#1E293B'
TEXT_MED = '#475569'
TEXT_LIGHT = '#94A3B8'


class TaskForgeExportController(http.Controller):

    # ──────────────────────────────────────────────────────────────────────────
    # Role-based Scope Helper
    # ──────────────────────────────────────────────────────────────────────────

    def _get_scoped_context(self):
        """
        Return (employee, role, team_ids, project_ids) based on logged-in user's role.
        - admin(CTO): all data
        - pl: own team (employees under this PL) + projects where they're PL
        - qr/ql: own taskers + projects where they're QR
        - tasker: only self + projects they're allocated to
        """
        user = request.env.user
        employee = user.employee_id
        if not employee:
            return None, '', [], []

        role = employee._get_task_forge_role()
        team_ids = employee._get_team_employee_ids()

        Project = request.env['project.project'].sudo()
        if role == 'admin':
            project_ids = Project.search([]).ids
        elif role == 'pl':
            project_ids = Project.search([
                '|', '|',
                ('project_lead', 'in', [employee.id]),
                ('project_qc_reviewer', 'in', team_ids),
                ('project_tasker', 'in', team_ids),
            ]).ids
        elif role in ('qr', 'ql'):
            project_ids = Project.search([
                '|',
                ('project_qc_reviewer', 'in', [employee.id]),
                ('project_tasker', 'in', team_ids),
            ]).ids
        else:
            project_ids = Project.search([('project_tasker', 'in', [employee.id])]).ids

        return employee, role, team_ids, project_ids

    # ──────────────────────────────────────────────────────────────────────────
    # Shared XLSX Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_formats(self, wb):
        """Return a dict of reusable xlsxwriter formats for a branded report."""
        fmt = {}

        fmt['title'] = wb.add_format({
            'bold': True, 'font_size': 18, 'font_color': WHITE,
            'bg_color': PRIMARY, 'align': 'left', 'valign': 'vcenter',
            'font_name': 'Calibri',
        })
        fmt['subtitle'] = wb.add_format({
            'italic': True, 'font_size': 10, 'font_color': TEXT_LIGHT,
            'bg_color': PRIMARY, 'align': 'left', 'valign': 'vcenter',
            'font_name': 'Calibri',
        })
        fmt['header'] = wb.add_format({
            'bold': True, 'font_size': 11, 'font_color': WHITE,
            'bg_color': ACCENT, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'text_wrap': True, 'font_name': 'Calibri',
        })
        fmt['cell'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'align': 'left', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'text_wrap': True, 'font_name': 'Calibri',
        })
        fmt['cell_center'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'font_name': 'Calibri',
        })
        fmt['cell_alt'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'bg_color': LIGHT_BG, 'align': 'left', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'text_wrap': True, 'font_name': 'Calibri',
        })
        fmt['cell_alt_center'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'bg_color': LIGHT_BG, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'font_name': 'Calibri',
        })
        fmt['number'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'num_format': '#,##0', 'font_name': 'Calibri',
        })
        fmt['number_alt'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'bg_color': LIGHT_BG, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'num_format': '#,##0', 'font_name': 'Calibri',
        })
        fmt['date_fmt'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'num_format': 'yyyy-mm-dd', 'font_name': 'Calibri',
        })
        fmt['date_fmt_alt'] = wb.add_format({
            'font_size': 10, 'font_color': TEXT_DARK,
            'bg_color': LIGHT_BG, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'num_format': 'yyyy-mm-dd', 'font_name': 'Calibri',
        })
        fmt['badge_success'] = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': WHITE,
            'bg_color': SUCCESS, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'font_name': 'Calibri',
        })
        fmt['badge_danger'] = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': WHITE,
            'bg_color': DANGER, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'font_name': 'Calibri',
        })
        fmt['badge_warning'] = wb.add_format({
            'bold': True, 'font_size': 10, 'font_color': WHITE,
            'bg_color': WARNING, 'align': 'center', 'valign': 'vcenter',
            'border': 1, 'border_color': BORDER_COLOR,
            'font_name': 'Calibri',
        })
        fmt['section'] = wb.add_format({
            'bold': True, 'font_size': 12, 'font_color': PRIMARY,
            'bottom': 2, 'bottom_color': ACCENT,
            'font_name': 'Calibri',
        })
        return fmt

    def _write_title_banner(self, ws, fmt, title, col_count):
        """Write a branded title banner across the top of the sheet."""
        now_str = datetime.now().strftime('%d %b %Y, %I:%M %p')
        ws.merge_range(0, 0, 0, col_count - 1, title, fmt['title'])
        ws.merge_range(1, 0, 1, col_count - 1, 'Downloaded on: %s' % now_str, fmt['subtitle'])
        ws.set_row(0, 36)
        ws.set_row(1, 20)

    def _write_headers(self, ws, fmt, headers, row=3):
        """Write styled column headers."""
        ws.set_row(row, 28)
        for col, h in enumerate(headers):
            ws.write(row, col, h, fmt['header'])

    def _write_row(self, ws, fmt, row_idx, values, col_types=None):
        """Write a row with alternating row colors. col_types: list of 'str'|'num'|'date'|'status'."""
        is_alt = row_idx % 2 == 0
        for col, val in enumerate(values):
            ctype = col_types[col] if col_types and col < len(col_types) else 'str'
            if ctype == 'status':
                status_str = str(val).lower() if val else ''
                if status_str in ('completed', 'active', 'live', 'no_issue', 'validated'):
                    ws.write(row_idx, col, val or '', fmt['badge_success'])
                elif status_str in ('blocker', 'escalated', 'critical', 'offboarded', 'overdue'):
                    ws.write(row_idx, col, val or '', fmt['badge_danger'])
                elif status_str in ('in_progress', 'pending', 'ack', 'testing', 'paused'):
                    ws.write(row_idx, col, val or '', fmt['badge_warning'])
                else:
                    ws.write(row_idx, col, val or '', fmt['cell_alt_center'] if is_alt else fmt['cell_center'])
            elif ctype == 'num':
                ws.write_number(row_idx, col, val if isinstance(val, (int, float)) else 0,
                                fmt['number_alt'] if is_alt else fmt['number'])
            elif ctype == 'date':
                ws.write(row_idx, col, str(val) if val else '',
                         fmt['date_fmt_alt'] if is_alt else fmt['date_fmt'])
            else:
                ws.write(row_idx, col, str(val) if val else '',
                         fmt['cell_alt'] if is_alt else fmt['cell'])

    def _finalize_and_upload(self, wb, output, report_name):
        """Close workbook, upload to S3, return link."""
        wb.close()
        file_bytes = output.getvalue()
        file_b64 = base64.b64encode(file_bytes).decode('utf-8')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = '%s_%s.xlsx' % (report_name, timestamp)
        try:
            s3_url = generate_s3_link(file_b64, prefix='reports', filename=filename)
            if s3_url:
                return s3_url
        except Exception as e:
            _logger.error('S3 upload failed for %s: %s', report_name, str(e))
        return ''

    def _unique_sheet_name(self, name, used_names):
        """Generate a unique sheet name (max 31 chars, case-insensitive dedup)."""
        base = (name or 'Sheet')[:28]
        candidate = base[:31]
        counter = 1
        while candidate.lower() in used_names:
            suffix = ' (%d)' % counter
            candidate = (base[:31 - len(suffix)] + suffix)
            counter += 1
        used_names.add(candidate.lower())
        return candidate

    def _get_date_filters(self, kwargs):
        """Extract start_date/end_date from kwargs. Returns (start_date, end_date) as strings or None."""
        return kwargs.get('start_date') or kwargs.get('date_from'), kwargs.get('end_date') or kwargs.get('date_to')

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Project Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/projects', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_projects(self, **kwargs):
        try:
            import xlsxwriter
            employee, role, team_ids, project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)

            employee = user_id.employee_id
            domain = [('non_stemp_project_status', 'in', ['not_started', 'production'])]
            if kwargs.get('show_all') in [1, '1']:
                domain = []
            if user_id.user_role.id == request.env.ref('api_auth_gateway.role_cto_technical').id:
                domain = []
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id,
                                          request.env.ref('api_auth_gateway.role_pl_stem').id,
                                          request.env.ref('api_auth_gateway.role_pl_non_stem').id]:
                domain.append(('project_lead', '=', employee.id))
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_qc_technical').id,
                                          request.env.ref('api_auth_gateway.role_qc_stem').id,
                                          request.env.ref('api_auth_gateway.role_qc_non_stem').id]:
                domain.append(('project_qc_reviewer', '=', employee.id))
            elif user_id.user_role.id in [request.env.ref('api_auth_gateway.role_tasker_technical').id,
                                          request.env.ref('api_auth_gateway.role_tasker_stem').id,
                                          request.env.ref('api_auth_gateway.role_tasker_non_stem').id]:
                domain.append(('project_tasker', '=', employee.id))
            start_date, end_date = self._get_date_filters(kwargs)
            if start_date:
                domain.append(('date_start', '>=', start_date))
            if end_date:
                domain.append(('date_start', '<=', end_date))
            projects = request.env['project.project'].sudo().search(domain)

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            ws = wb.add_worksheet('Projects')

            headers = ['#', 'Project Name', 'Project Seq', 'Category', 'Status',
                        'PL Count', 'QR Count', 'Tasker Count', 'SWE Count',
                        'Start Date', 'Tasks', 'Blockers']
            # Platform
            col_types = ['num', 'str', 'str', 'str', 'status',
                         'num', 'num', 'num', 'num',
                         'date', 'num', 'num']
            # str
            widths = [5, 35, 15, 12, 12, 10, 10, 12, 10, 12, 8, 10, 14]

            self._write_title_banner(ws, fmt, 'Project Report', len(headers))
            self._write_headers(ws, fmt, headers)
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            for idx, p in enumerate(projects, 1):
                task_count = TaskLog.search_count([('project_id', '=', p.id)])
                blocker_count = Blocker.search_count([
                    ('project_id', '=', p.id), ('state', 'not in', ['no_issue', 'resolved'])])
                row = [
                    idx,
                    p.name or '',
                    p.project_seq or '',
                    p.project_category or '',
                    p.non_stemp_project_status or p.stage_id.name or '',
                    len(p.project_lead),
                    len(p.project_qc_reviewer),
                    len(p.project_tasker),
                    len(p.project_swe),
                    str(p.date_start) if p.date_start else '',
                    task_count,
                    blocker_count,
                    # p.task_forge_platform or '',
                ]
                self._write_row(ws, fmt, idx + 3, row, col_types)

            ws.autofilter(3, 0, 3 + len(projects), len(headers) - 1)
            ws.freeze_panes(4, 2)
            s3_url = self._finalize_and_upload(wb, output, 'project_report')

            return return_Response(message="Project report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export projects failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Task Log Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/tasks', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_tasks(self, **kwargs):
        try:
            import xlsxwriter
            employee, role, team_ids, project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            TaskLog = request.env['task.forge.log'].sudo()

            domain = [('employee_id', 'in', team_ids)]
            start_date, end_date = self._get_date_filters(kwargs)
            if start_date:
                domain.append(('date', '>=', start_date))
            if end_date:
                domain.append(('date', '<=', end_date))
            if kwargs.get('project_id'):
                pid = int(kwargs['project_id'])
                if pid not in project_ids:
                    return return_Response(message="Access denied: project not in your scope", status=403)
                domain.append(('project_id', '=', pid))
            else:
                domain.append(('project_id', 'in', project_ids))

            tasks = TaskLog.search(domain, order='date desc, create_date desc')

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            ws = wb.add_worksheet('Task Logs')

            headers = ['#', 'Reference', 'Task Name', 'Tasker', 'Project', 'Date',
                        'Status', 'Start Time', 'End Time', 'Duration (min)',
                        'Quality Score', 'Task Score', 'Blocker Reason']
            col_types = ['num', 'str', 'str', 'str', 'str', 'date',
                         'status', 'str', 'str', 'num',
                         'num', 'num', 'str']
            widths = [5, 12, 30, 20, 25, 12, 14, 18, 18, 14, 13, 11, 30]

            self._write_title_banner(ws, fmt, 'Task Log Report', len(headers))
            self._write_headers(ws, fmt, headers)
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            for idx, t in enumerate(tasks, 1):
                row = [
                    idx,
                    t.sequence or '',
                    t.name or '',
                    t.employee_id.name if t.employee_id else '',
                    t.project_id.name if t.project_id else '',
                    str(t.date) if t.date else '',
                    t.state or '',
                    t.start_time.strftime('%Y-%m-%d %H:%M') if t.start_time else '',
                    t.end_time.strftime('%Y-%m-%d %H:%M') if t.end_time else '',
                    # t.time_taken_mins or 0,
                    round(int(t.pause_time) / 60, 2) if t.pause_time else 0,
                    t.quality_score or 0,
                    t.task_score or 0,
                    t.blocker_reason or '',
                ]
                self._write_row(ws, fmt, idx + 3, row, col_types)

            ws.autofilter(3, 0, 3 + len(tasks), len(headers) - 1)
            ws.freeze_panes(4, 3)
            s3_url = self._finalize_and_upload(wb, output, 'task_log_report')

            return return_Response(message="Task log report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export tasks failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Blocker Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/blockers', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_blockers(self, **kwargs):
        try:
            import xlsxwriter
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Blocker = request.env['task.forge.blocker'].sudo()
            role = employee._get_task_forge_role()

            if role == 'admin':
                domain = []
                if kwargs.get('active_blocker') in [1, '1']:
                    domain = [('state', 'not in', ['no_issue', 'resolved'])]
            elif role == 'pl':
                team_ids = employee._get_team_employee_ids()
                domain = [
                    '|',
                    ('employee_id', 'in', team_ids),
                    ('state', '=', 'escalated_to_pl'),
                ]
                if kwargs.get('active_blocker') in [1, '1']:
                    domain.append(('state', 'not in', ['no_issue', 'resolved']))
            elif role in ('qr', 'ql'):
                domain = [('qr_id', '=', employee.id)]
                if kwargs.get('active_blocker') in [1, '1']:
                    domain.append(('state', 'not in', ['no_issue', 'resolved']))
            else:
                domain = [('employee_id', '=', employee.id)]
                if kwargs.get('active_blocker') in [1, '1']:
                    domain.append(('state', 'not in', ['no_issue', 'resolved']))

            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs.get('project_id'))))
            if kwargs.get('search'):
                domain.append(('name', 'ilike', kwargs.get('search')))
            if kwargs.get('state'):
                domain.append(('state', 'in', [kwargs.get('status')]))
            if kwargs.get('priority'):
                domain.append(('priority', '=', kwargs.get('priority')))
            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs.get('employee_id'))))
            if kwargs.get('assignee'):
                domain.append('|')
                domain.append(('qr_id', '=', int(kwargs.get('assignee'))))
                domain.append(('pl_id', '=', int(kwargs.get('assignee'))))

            start_date, end_date = self._get_date_filters(kwargs)
            if start_date:
                domain.append(('create_date', '>=', start_date))
            if end_date:
                domain.append(('create_date', '<=', end_date))

            blockers = Blocker.search(domain, order='create_date desc')

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            ws = wb.add_worksheet('Blockers')

            headers = ['#', 'Summary', 'Task', 'Project', 'Raised By', 'QR',
                        'PL', 'Priority', 'Status', 'Blocker Type', 'Reason',
                        'QR Notes', 'Created']
            col_types = ['num', 'str', 'str', 'str', 'str', 'str',
                         'str', 'status', 'status', 'str', 'str',
                         'str', 'date']
            widths = [5, 30, 25, 25, 18, 18, 18, 10, 12, 14, 35, 30, 12]

            self._write_title_banner(ws, fmt, 'Blocker Report', len(headers))
            self._write_headers(ws, fmt, headers)
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            priority_map = {'0': 'Low', '1': 'Medium', '2': 'High', '3': 'Critical'}

            for idx, b in enumerate(blockers, 1):
                row = [
                    idx,
                    b.name or '',
                    b.task_id.name if b.task_id else '',
                    b.project_id.name if b.project_id else '',
                    b.employee_id.name if b.employee_id else '',
                    b.qr_id.name if b.qr_id else '',
                    b.pl_id.name if b.pl_id else '',
                    priority_map.get(b.priority, b.priority or ''),
                    b.state or '',
                    b.blocker_type or '',
                    b.blocker_reason or '',
                    b.qr_notes or '',
                    b.create_date.strftime('%Y-%m-%d') if b.create_date else '',
                ]
                self._write_row(ws, fmt, idx + 3, row, col_types)

            ws.autofilter(3, 0, 3 + len(blockers), len(headers) - 1)
            ws.freeze_panes(4, 2)
            s3_url = self._finalize_and_upload(wb, output, 'blocker_report')

            return return_Response(message="Blocker report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export blockers failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Team Overview Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/team_overview', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_team_overview(self, **kwargs):
        try:
            import xlsxwriter
            emp, role, team_ids, project_ids = self._get_scoped_context()
            if not emp:
                return return_Response(message="Employee profile not found", status=404)

            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            employees = Employee.browse(team_ids)

            start_date, end_date = self._get_date_filters(kwargs)
            date_domain = []
            if start_date:
                date_domain.append(('date', '>=', start_date))
            if end_date:
                date_domain.append(('date', '<=', end_date))
            blocker_date_domain = []
            if start_date:
                blocker_date_domain.append(('create_date', '>=', start_date))
            if end_date:
                blocker_date_domain.append(('create_date', '<=', end_date))

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            ws = wb.add_worksheet('Team Overview')

            headers = ['#', 'Employee Name', 'Email', 'Role', 'PL', 'QR',
                        'Total Tasks', 'Completed', 'In Progress', 'Blockers Raised',
                        'Avg Quality Score', 'Allocation Status']
            col_types = ['num', 'str', 'str', 'str', 'str', 'str',
                         'num', 'num', 'num', 'num',
                         'num', 'status']
            widths = [5, 25, 28, 14, 20, 20, 12, 12, 12, 15, 16, 16]

            self._write_title_banner(ws, fmt, 'Team Overview Report', len(headers))
            self._write_headers(ws, fmt, headers)
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            for idx, emp in enumerate(employees, 1):
                # role = emp._get_task_forge_role() if hasattr(emp, '_get_task_forge_role') else ''
                role = emp.user_id.user_role.name or ''
                total_tasks = TaskLog.search_count([('employee_id', '=', emp.id)] + date_domain)
                completed = TaskLog.search_count([('employee_id', '=', emp.id), ('state', '=', 'completed')] + date_domain)
                in_progress = TaskLog.search_count([('employee_id', '=', emp.id), ('state', '=', 'in_progress')] + date_domain)
                blocker_count = Blocker.search_count([('employee_id', '=', emp.id)] + blocker_date_domain)

                scores = TaskLog.search([
                    ('employee_id', '=', emp.id),
                    ('quality_score', '>', 0),
                ] + date_domain)
                avg_score = 0
                if scores:
                    avg_score = round(sum(s.quality_score for s in scores) / len(scores), 1)

                # alloc_status = getattr(emp, 'tf_allocation_status', '') or ''
                active_projects = request.env['project.project'].sudo().search_count(['|', '|', ('project_tasker', '=', emp.id), ('project_qc_reviewer', '=', emp.id), ('project_lead', '=', emp.id), ('non_stemp_project_status', 'in', ['not_started', 'production'])])

                alloc_status = "Allocated" if active_projects else 'On-Bench'

                row = [
                    idx,
                    emp.name or '',
                    emp.work_email or '',
                    role,
                    emp.task_forge_pl_id.name if emp.task_forge_pl_id else '',
                    emp.task_forge_qr_id.name if emp.task_forge_qr_id else '',
                    total_tasks,
                    completed,
                    in_progress,
                    blocker_count,
                    avg_score,
                    alloc_status,
                ]
                self._write_row(ws, fmt, idx + 3, row, col_types)

            ws.autofilter(3, 0, 3 + len(employees), len(headers) - 1)
            ws.freeze_panes(4, 2)
            s3_url = self._finalize_and_upload(wb, output, 'team_overview_report')

            return return_Response(message="Team overview report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export team overview failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Project‑wise Team Overview Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/project_team', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_project_team(self, **kwargs):
        try:
            import xlsxwriter
            employee, role, team_ids, scoped_project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Project = request.env['project.project'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Employee = request.env['hr.employee'].sudo()

            domain = [('id', 'in', scoped_project_ids)]
            if kwargs.get('project_id'):
                pid = int(kwargs['project_id'])
                if pid not in scoped_project_ids:
                    return return_Response(message="Access denied: project not in your scope", status=403)
                domain = [('id', '=', pid)]
            projects = Project.search(domain, order='name asc')

            start_date, end_date = self._get_date_filters(kwargs)
            date_domain = []
            if start_date:
                date_domain.append(('date', '>=', start_date))
            if end_date:
                date_domain.append(('date', '<=', end_date))
            blocker_date_domain = []
            if start_date:
                blocker_date_domain.append(('create_date', '>=', start_date))
            if end_date:
                blocker_date_domain.append(('create_date', '<=', end_date))

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            used_sheet_names = set()

            for proj in projects:
                sheet_name = self._unique_sheet_name(proj.name, used_sheet_names)
                ws = wb.add_worksheet(sheet_name)

                headers = ['#', 'Employee Name', 'Role', 'PL', 'QR',
                            'Tasks Done', 'In Progress', 'Quality Avg', 'Blockers']
                col_types = ['num', 'str', 'str', 'str', 'str',
                             'num', 'num', 'num', 'num']
                widths = [5, 25, 14, 20, 20, 12, 12, 12, 10]

                self._write_title_banner(ws, fmt, '%s — Team' % (proj.name or ''), len(headers))
                self._write_headers(ws, fmt, headers)
                for i, w in enumerate(widths):
                    ws.set_column(i, i, w)

                members = set()
                for field in ('project_lead', 'project_qc_reviewer', 'project_tasker', 'project_aire', 'project_swe'):
                    members.update(proj[field].ids)
                members = members.intersection(set(team_ids))

                role_map = {}
                for emp_id in proj.project_lead.ids:
                    role_map[emp_id] = 'Lead'
                for emp_id in proj.project_qc_reviewer.ids:
                    role_map[emp_id] = 'QC Reviewer'
                for emp_id in proj.project_tasker.ids:
                    role_map[emp_id] = 'Tasker'
                for emp_id in proj.project_aire.ids:
                    role_map[emp_id] = 'AIRE'
                for emp_id in proj.project_swe.ids:
                    role_map[emp_id] = 'SWE'

                Blocker = request.env['task.forge.blocker'].sudo()
                member_emps = Employee.browse(list(members))

                for idx, emp in enumerate(member_emps, 1):
                    done = TaskLog.search_count([
                        ('employee_id', '=', emp.id),
                        ('project_id', '=', proj.id),
                        ('state', '=', 'completed'),
                    ] + date_domain)
                    ip = TaskLog.search_count([
                        ('employee_id', '=', emp.id),
                        ('project_id', '=', proj.id),
                        ('state', '=', 'in_progress'),
                    ] + date_domain)
                    scores = TaskLog.search([
                        ('employee_id', '=', emp.id),
                        ('project_id', '=', proj.id),
                        ('quality_score', '>', 0),
                    ] + date_domain)
                    avg_q = round(sum(s.quality_score for s in scores) / len(scores), 1) if scores else 0
                    blk = Blocker.search_count([
                        ('employee_id', '=', emp.id),
                        ('project_id', '=', proj.id),
                    ] + blocker_date_domain)

                    row = [
                        idx,
                        emp.name or '',
                        role_map.get(emp.id, ''),
                        emp.task_forge_pl_id.name if emp.task_forge_pl_id else '',
                        emp.task_forge_qr_id.name if emp.task_forge_qr_id else '',
                        done,
                        ip,
                        avg_q,
                        blk,
                    ]
                    self._write_row(ws, fmt, idx + 3, row, col_types)

                ws.autofilter(3, 0, 3 + len(members), len(headers) - 1)
                ws.freeze_panes(4, 2)

            s3_url = self._finalize_and_upload(wb, output, 'project_team_report')
            return return_Response(message="Project team report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export project team failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Project‑wise Blocker Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/project_blockers', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_project_blockers(self, **kwargs):
        try:
            import xlsxwriter
            employee, role, team_ids, scoped_project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Project = request.env['project.project'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            domain = [('id', 'in', scoped_project_ids)]
            if kwargs.get('project_id'):
                pid = int(kwargs['project_id'])
                if pid not in scoped_project_ids:
                    return return_Response(message="Access denied: project not in your scope", status=403)
                domain = [('id', '=', pid)]
            projects = Project.search(domain, order='name asc')

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)

            priority_map = {'0': 'Low', '1': 'Medium', '2': 'High', '3': 'Critical'}
            used_sheet_names = set()

            start_date, end_date = self._get_date_filters(kwargs)

            for proj in projects:
                blocker_domain = [('project_id', '=', proj.id)]
                if role == 'pl':
                    blocker_domain.append(('employee_id', 'in', team_ids))
                elif role in ('qr', 'ql'):
                    blocker_domain.extend(['|', ('qr_id', '=', employee.id), ('employee_id', 'in', team_ids)])
                elif role == 'tasker':
                    blocker_domain.append(('employee_id', '=', employee.id))
                if start_date:
                    blocker_domain.append(('create_date', '>=', start_date))
                if end_date:
                    blocker_domain.append(('create_date', '<=', end_date))
                blockers = Blocker.search(blocker_domain, order='create_date desc')
                if not blockers:
                    continue

                sheet_name = self._unique_sheet_name(proj.name, used_sheet_names)
                ws = wb.add_worksheet(sheet_name)

                headers = ['#', 'Summary', 'Task', 'Raised By', 'QR', 'PL',
                            'Priority', 'Status', 'Type', 'Reason',
                            'QR Notes', 'Created']
                col_types = ['num', 'str', 'str', 'str', 'str', 'str',
                             'status', 'status', 'str', 'str',
                             'str', 'date']
                widths = [5, 28, 22, 18, 18, 18, 10, 12, 12, 30, 28, 12]

                self._write_title_banner(ws, fmt, '%s — Blockers' % (proj.name or ''), len(headers))
                self._write_headers(ws, fmt, headers)
                for i, w in enumerate(widths):
                    ws.set_column(i, i, w)

                for idx, b in enumerate(blockers, 1):
                    row = [
                        idx,
                        b.name or '',
                        b.task_id.name if b.task_id else '',
                        b.employee_id.name if b.employee_id else '',
                        b.qr_id.name if b.qr_id else '',
                        b.pl_id.name if b.pl_id else '',
                        priority_map.get(b.priority, b.priority or ''),
                        b.state or '',
                        b.blocker_type or '',
                        b.blocker_reason or '',
                        b.qr_notes or '',
                        b.create_date.strftime('%Y-%m-%d') if b.create_date else '',
                    ]
                    self._write_row(ws, fmt, idx + 3, row, col_types)

                ws.autofilter(3, 0, 3 + len(blockers), len(headers) - 1)
                ws.freeze_panes(4, 2)

            s3_url = self._finalize_and_upload(wb, output, 'project_blocker_report')
            return return_Response(message="Project blocker report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export project blockers failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Employee List Report
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/employees', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_employee_list(self, **kwargs):
        try:
            import xlsxwriter
            Employee = request.env['hr.employee'].sudo()
            TaskLog = request.env['task.forge.log'].sudo()
            Blocker = request.env['task.forge.blocker'].sudo()

            domain = [('task_forge_active', '=', True)]

            if kwargs.get('role'):
                domain.append(('user_id.user_role', '=', int(kwargs.get('role'))))

            if kwargs.get('search'):
                domain += ['|', ('name', 'ilike', kwargs['search']), ('work_email', 'ilike', kwargs['search'])]

            start_date, end_date = self._get_date_filters(kwargs)
            task_date_domain = []
            if start_date:
                task_date_domain.append(('date', '>=', start_date))
            if end_date:
                task_date_domain.append(('date', '<=', end_date))
            blocker_date_domain = []
            if start_date:
                blocker_date_domain.append(('create_date', '>=', start_date))
            if end_date:
                blocker_date_domain.append(('create_date', '<=', end_date))

            employees = Employee.search(domain, order='name asc')

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)
            ws = wb.add_worksheet('Employee List')

            headers = ['#', 'Employee Name', 'Email', 'Role', 'PL', 'QR',
                        'Date of Joining', 'Total Tasks', 'Completed', 'Blockers',
                        'Avg Quality', 'Status', 'Job Title']
            col_types = ['num', 'str', 'str', 'str', 'str', 'str',
                         'date', 'num', 'num', 'num',
                         'num', 'status', 'str']
            widths = [5, 28, 30, 16, 20, 20, 14, 12, 12, 10, 13, 14, 20]

            self._write_title_banner(ws, fmt, 'Employee List Report', len(headers))
            self._write_headers(ws, fmt, headers)
            for i, w in enumerate(widths):
                ws.set_column(i, i, w)

            for idx, emp in enumerate(employees, 1):
                role_name = emp.user_id.user_role.name if emp.user_id and emp.user_id.user_role else ''
                total_tasks = TaskLog.search_count([('employee_id', '=', emp.id)] + task_date_domain)
                completed = TaskLog.search_count([('employee_id', '=', emp.id), ('state', '=', 'completed')] + task_date_domain)
                blocker_count = Blocker.search_count([('employee_id', '=', emp.id)] + blocker_date_domain)
                scores = TaskLog.search([('employee_id', '=', emp.id), ('quality_score', '>', 0)] + task_date_domain)
                avg_score = round(sum(s.quality_score for s in scores) / len(scores), 1) if scores else 0

                active_projects = request.env['project.project'].sudo().search_count([
                    '|', '|',
                    ('project_tasker', '=', emp.id),
                    ('project_qc_reviewer', '=', emp.id),
                    ('project_lead', '=', emp.id),
                    ('non_stemp_project_status', 'in', ['not_started', 'production']),
                ])
                alloc_status = 'Allocated' if active_projects else 'On-Bench'

                row = [
                    idx,
                    emp.name or '',
                    emp.work_email or '',
                    role_name,
                    emp.task_forge_pl_id.name if emp.task_forge_pl_id else '',
                    emp.task_forge_qr_id.name if emp.task_forge_qr_id else '',
                    emp.create_date.strftime('%Y-%m-%d') if emp.create_date else '',
                    total_tasks,
                    completed,
                    blocker_count,
                    avg_score,
                    alloc_status,
                    emp.designation_id.name if emp.designation_id else '',
                ]
                self._write_row(ws, fmt, idx + 3, row, col_types)

            ws.autofilter(3, 0, 3 + len(employees), len(headers) - 1)
            ws.freeze_panes(4, 2)
            s3_url = self._finalize_and_upload(wb, output, 'employee_list_report')

            return return_Response(message="Employee list report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export employee list failed: %s', str(e))
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Blocker Detail Report (single blocker with escalation history)
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/export/blocker_detail', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def export_blocker_detail(self, **kwargs):
        try:
            import xlsxwriter
            employee, role, team_ids, project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            blocker_id = kwargs.get('blocker_id')
            if not blocker_id:
                return return_Response(message="blocker_id is required", status=400)

            Blocker = request.env['task.forge.blocker'].sudo()
            blocker = Blocker.browse(int(blocker_id))
            if not blocker.exists():
                return return_Response(message="Blocker not found", status=404)

            output = io.BytesIO()
            wb = xlsxwriter.Workbook(output, {'in_memory': True})
            fmt = self._get_formats(wb)

            # Extra formats for this report
            fmt['kv_label'] = wb.add_format({
                'bold': True, 'font_size': 11, 'font_color': '#1E293B',
                'bg_color': '#E2E8F0', 'align': 'right', 'valign': 'vcenter',
                'border': 1, 'border_color': '#CBD5E1', 'font_name': 'Calibri',
            })
            fmt['kv_value'] = wb.add_format({
                'font_size': 11, 'font_color': '#1E293B',
                'align': 'left', 'valign': 'vcenter', 'text_wrap': True,
                'border': 1, 'border_color': '#CBD5E1', 'font_name': 'Calibri',
            })
            fmt['kv_value_bold'] = wb.add_format({
                'bold': True, 'font_size': 11, 'font_color': '#1E293B',
                'align': 'left', 'valign': 'vcenter', 'text_wrap': True,
                'border': 1, 'border_color': '#CBD5E1', 'font_name': 'Calibri',
            })
            fmt['section_header'] = wb.add_format({
                'bold': True, 'font_size': 13, 'font_color': '#FFFFFF',
                'bg_color': '#1B2A4A', 'align': 'left', 'valign': 'vcenter',
                'border': 1, 'border_color': '#CBD5E1', 'font_name': 'Calibri',
            })
            fmt['url_fmt'] = wb.add_format({
                'font_size': 10, 'font_color': '#2E86DE', 'underline': True,
                'align': 'left', 'valign': 'vcenter', 'text_wrap': True,
                'border': 1, 'border_color': '#CBD5E1', 'font_name': 'Calibri',
            })
            fmt['timeline_arrow'] = wb.add_format({
                'bold': True, 'font_size': 14, 'font_color': '#2E86DE',
                'align': 'center', 'valign': 'vcenter', 'font_name': 'Calibri',
            })

            priority_map = {'0': 'Low', '1': 'Medium', '2': 'High', '3': 'Critical'}
            state_map = {
                'pending': 'Pending', 'no_issue': 'No Issue',
                'escalated_to_pl': 'Escalated to PL', 'escalated_to_cto': 'Escalated to CTO',
                'resolved': 'Resolved', 'validated': 'Validated as Bug',
                'ack': 'Acknowledged', 'escalated': 'Escalated',
            }
            action_map = {
                'create': 'Created', 'no_issue': 'Marked No Issue',
                'escalate': 'Escalated', 'resolve': 'Resolved',
                'validate_bug': 'Validated as Bug',
            }
            role_map = {'tasker': 'Tasker', 'qr': 'QR', 'pl': 'PL', 'cto': 'CTO', '': '—'}

            # ── Sheet 1: Blocker Overview ─────────────────────────────────────
            ws = wb.add_worksheet('Blocker Overview')
            ws.set_column(0, 0, 22)
            ws.set_column(1, 1, 55)
            ws.set_column(2, 2, 22)
            ws.set_column(3, 3, 55)

            self._write_title_banner(ws, fmt, 'Blocker Detail Report — #%s' % blocker.id, 4)

            row = 3

            # Section: Basic Info
            ws.merge_range(row, 0, row, 3, '  Blocker Information', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            info_rows = [
                ('Blocker ID', str(blocker.id)),
                ('Summary', blocker.name or ''),
                ('Status', state_map.get(blocker.state, blocker.state or '')),
                ('Priority', priority_map.get(blocker.priority, blocker.priority or '')),
                ('Blocker Type', blocker.blocker_type or ''),
                ('Escalation Level', (blocker.escalation_level or 'qr').upper()),
                ('Created', blocker.create_date.strftime('%d %b %Y, %I:%M %p') if blocker.create_date else ''),
            ]
            for label, value in info_rows:
                ws.write(row, 0, label, fmt['kv_label'])
                ws.write(row, 1, value, fmt['kv_value_bold'] if label in ('Summary', 'Status') else fmt['kv_value'])
                row += 1

            row += 1

            # Section: People
            ws.merge_range(row, 0, row, 3, '  People', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            people_rows = [
                ('Raised By', blocker.employee_id.name if blocker.employee_id else '', 'QR', blocker.qr_id.name if blocker.qr_id else ''),
                ('PL', blocker.pl_id.name if blocker.pl_id else '', 'Resolved By', blocker.resolved_by_id.name if blocker.resolved_by_id else ''),
            ]
            for l1, v1, l2, v2 in people_rows:
                ws.write(row, 0, l1, fmt['kv_label'])
                ws.write(row, 1, v1, fmt['kv_value'])
                ws.write(row, 2, l2, fmt['kv_label'])
                ws.write(row, 3, v2, fmt['kv_value'])
                row += 1

            row += 1

            # Section: Task & Project
            ws.merge_range(row, 0, row, 3, '  Task & Project', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            ws.write(row, 0, 'Task', fmt['kv_label'])
            ws.write(row, 1, '%s (%s)' % (blocker.task_id.name or '', blocker.task_id.sequence or '') if blocker.task_id else '', fmt['kv_value'])
            ws.write(row, 2, 'Project', fmt['kv_label'])
            ws.write(row, 3, blocker.project_id.name if blocker.project_id else '', fmt['kv_value'])
            row += 2

            # Section: Blocker Reason & Notes
            ws.merge_range(row, 0, row, 3, '  Details & Notes', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            detail_rows = [
                ('Blocker Reason', blocker.blocker_reason or ''),
                ('QR Notes', blocker.qr_notes or ''),
                ('PL Notes', blocker.pl_notes or ''),
                ('CTO Notes', blocker.cto_notes or ''),
                ('Resolution Notes', blocker.resolution_notes or ''),
            ]
            for label, value in detail_rows:
                ws.write(row, 0, label, fmt['kv_label'])
                ws.merge_range(row, 1, row, 3, value, fmt['kv_value'])
                if value:
                    ws.set_row(row, max(20, min(80, len(value) // 3)))
                row += 1

            row += 1

            # Section: Timestamps
            ws.merge_range(row, 0, row, 3, '  Timeline', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            ts_format = '%d %b %Y, %I:%M %p'
            timestamps = [
                ('Created', blocker.create_date.strftime(ts_format) if blocker.create_date else '', 'QR Action', blocker.qr_action_at.strftime(ts_format) if blocker.qr_action_at else ''),
                ('PL Action', blocker.pl_action_at.strftime(ts_format) if blocker.pl_action_at else '', 'CTO Action', blocker.cto_action_at.strftime(ts_format) if blocker.cto_action_at else ''),
                ('Resolved At', blocker.resolved_at.strftime(ts_format) if blocker.resolved_at else '', 'PL Validated', blocker.pl_validated_at.strftime(ts_format) if blocker.pl_validated_at else ''),
            ]
            for l1, v1, l2, v2 in timestamps:
                ws.write(row, 0, l1, fmt['kv_label'])
                ws.write(row, 1, v1, fmt['kv_value'])
                ws.write(row, 2, l2, fmt['kv_label'])
                ws.write(row, 3, v2, fmt['kv_value'])
                row += 1

            row += 1

            # Section: Attachments
            ws.merge_range(row, 0, row, 3, '  Attachments & Media', fmt['section_header'])
            ws.set_row(row, 26)
            row += 1

            attachments = [
                ('Blocker Image', blocker.blocker_image_url or ''),
                ('QR Image', blocker.qr_image_url or ''),
                ('QR Video', blocker.qr_video_url or ''),
                ('PL Image', blocker.pl_image_url or ''),
                ('CTO Image', blocker.cto_image_url or ''),
            ]
            for label, url in attachments:
                ws.write(row, 0, label, fmt['kv_label'])
                if url:
                    ws.write_url(row, 1, url, fmt['url_fmt'], string='View / Download')
                else:
                    ws.merge_range(row, 1, row, 3, '—', fmt['kv_value'])
                row += 1

            # Validated Bug
            if blocker.validated_bug_id:
                row += 1
                ws.merge_range(row, 0, row, 3, '  Validated Bug', fmt['section_header'])
                ws.set_row(row, 26)
                row += 1
                bug = blocker.validated_bug_id
                bug_rows = [
                    ('Bug ID', str(bug.id)),
                    ('Title', bug.name or ''),
                    ('Impact', bug.impact or ''),
                    ('State', bug.state or ''),
                    ('Description', bug.bug_description or ''),
                ]
                for label, value in bug_rows:
                    ws.write(row, 0, label, fmt['kv_label'])
                    ws.merge_range(row, 1, row, 3, value, fmt['kv_value'])
                    row += 1

            # ── Sheet 2: Escalation Timeline ──────────────────────────────────
            ws2 = wb.add_worksheet('Escalation Timeline')

            headers = ['#', 'Action', 'From', 'To', 'Action By', 'Notes', 'Image', 'Documents', 'Time']
            col_types = ['num', 'str', 'str', 'str', 'str', 'str', 'str', 'str', 'str']
            widths = [5, 18, 10, 10, 22, 40, 30, 35, 22]

            self._write_title_banner(ws2, fmt, 'Escalation Timeline — %s' % (blocker.name or ''), len(headers))
            self._write_headers(ws2, fmt, headers)
            for i, w in enumerate(widths):
                ws2.set_column(i, i, w)

            logs = blocker.escalation_log_ids.sorted('create_date')
            for idx, log in enumerate(logs, 1):
                doc_urls = log.document_urls or ''
                row_data = [
                    idx,
                    action_map.get(log.action, log.action or ''),
                    role_map.get(log.from_role, log.from_role or ''),
                    role_map.get(log.to_role, log.to_role or ''),
                    log.action_by_name or '',
                    log.notes or '',
                    log.image_url or '',
                    doc_urls,
                    log.create_date.strftime('%d %b %Y, %I:%M %p') if log.create_date else '',
                ]
                self._write_row(ws2, fmt, idx + 3, row_data, col_types)

            ws2.autofilter(3, 0, 3 + len(logs), len(headers) - 1)
            ws2.freeze_panes(4, 2)

            s3_url = self._finalize_and_upload(wb, output, 'blocker_detail_%s' % blocker.id)

            return return_Response(message="Blocker detail report generated", status=200,
                                   data={'download_url': s3_url})
        except Exception as e:
            _logger.error('Export blocker detail failed: %s', str(e))
            return return_Response(message=str(e), status=400)
