from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
import json
import base64


class TaskForgeBugController(http.Controller):

    @http.route('/api/v2/taskforge/validated_bugs', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_validated_bugs(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Bug = request.env['task.forge.validated.bug'].sudo()
            bugs = Bug.search([], order='create_date desc', limit=200)

            data = [{
                'id': b.id,
                'name': b.name,
                'project_name': b.project_name or '',
                'employee_name': b.employee_name or '',
                'impact': b.impact,
                'state': b.state,
                'bug_description': b.bug_description or '',
                'steps_to_reproduce': b.steps_to_reproduce or '',
                'pages_affected': b.pages_affected or '',
                'impact_details': b.impact_details or '',
                'blocker_reason': b.blocker_reason or '',
                'qr_video_url': b.qr_video_url or '',
                'qr_image_url': b.qr_image_url or '',
                'validated_by': b.validated_by_id.name if b.validated_by_id else '',
                'created_at': b.create_date.isoformat() if b.create_date else '',
            } for b in bugs]

            return return_Response(message="Validated bugs", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/bug_reports', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_bug_reports(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            BugReport = request.env['task.forge.bug.report'].sudo()
            team_ids = employee._get_team_employee_ids()
            reports = BugReport.search([('employee_id', 'in', team_ids)], order='create_date desc', limit=200)

            data = [{
                'id': r.id,
                'name': r.name,
                'task_id': r.task_id.id,
                'task_name': r.task_name or '',
                'employee_id': r.employee_id.id,
                'employee_name': r.employee_name or '',
                'video_url': r.video_url or '',
                'description': r.description or '',
                'state': r.state,
                'created_at': r.create_date.isoformat() if r.create_date else '',
            } for r in reports]

            return return_Response(message="Bug reports", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/bug_reports', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'task_id': {'type': 'int', 'required': True},
        'description': {'type': 'string', 'required': False},
    })
    def create_bug_report(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            # Handle video upload
            video_url = ''
            video_file = request.httprequest.files.get('video')
            if video_file:
                vid_data = base64.b64encode(video_file.read())
                video_url = generate_s3_link(vid_data, prefix='taskforge/bug_videos', uid=employee.id, filename=video_file.filename)

            TaskLog = request.env['task.forge.log'].sudo()
            task = TaskLog.browse(jdata['task_id'])

            BugReport = request.env['task.forge.bug.report'].sudo()
            report = BugReport.create({
                'task_id': task.id,
                'employee_id': employee.id,
                'qr_id': employee.task_forge_qr_id.id if employee.task_forge_qr_id else False,
                'pl_id': employee.task_forge_pl_id.id if employee.task_forge_pl_id else False,
                'video_url': video_url,
                'description': jdata.get('description', ''),
            })

            return return_Response(
                message="Bug report created",
                status=200,
                data={'data': {
                    'id': report.id,
                    'name': report.name,
                    'task_id': report.task_id.id,
                    'video_url': report.video_url or '',
                    'state': report.state,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/bug_reports/delete', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'bug_report_id': {'type': 'int', 'required': True},
    })
    def delete_bug_report(self, jdata=None, **kwargs):
        try:
            BugReport = request.env['task.forge.bug.report'].sudo()
            report = BugReport.browse(jdata['bug_report_id'])
            if not report.exists():
                return return_Response(message="Bug report not found", status=404)

            # Revert task to Completed if it was in Blocker state
            if report.task_id and report.task_id.state == 'blocker':
                report.task_id.write({'state': 'completed'})

            report.unlink()
            return return_Response(message="Bug report deleted", status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)
