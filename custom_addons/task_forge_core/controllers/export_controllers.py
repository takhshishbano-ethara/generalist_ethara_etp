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

            Project = request.env['project.project'].sudo()
            proj_domain = [('id', 'in', project_ids)]
            start_date, end_date = self._get_date_filters(kwargs)
            if start_date:
                proj_domain.append(('date_start', '>=', start_date))
            if end_date:
                proj_domain.append(('date_start', '<=', end_date))
            projects = Project.search(proj_domain, order='name asc')

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
                    ('project_id', '=', p.id), ('state', 'not in', ['no_issue'])])
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
            employee, role, team_ids, project_ids = self._get_scoped_context()
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Blocker = request.env['task.forge.blocker'].sudo()

            if role == 'admin':
                domain = []
            elif role == 'pl':
                domain = [('employee_id', 'in', team_ids)]
            elif role in ('qr', 'ql'):
                domain = ['|', ('qr_id', '=', employee.id), ('employee_id', 'in', team_ids)]
            else:
                domain = [('employee_id', '=', employee.id)]

            if kwargs.get('project_id'):
                pid = int(kwargs['project_id'])
                if pid not in project_ids:
                    return return_Response(message="Access denied: project not in your scope", status=403)
                domain.append(('project_id', '=', pid))
            else:
                domain.append(('project_id', 'in', project_ids))

            if kwargs.get('state'):
                domain.append(('state', '=', kwargs['state']))

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
