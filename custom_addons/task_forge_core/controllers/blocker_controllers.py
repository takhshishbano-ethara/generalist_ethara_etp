from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
import json


class TaskForgeBlockerController(http.Controller):

    @http.route('/api/v2/taskforge/blockers', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_blockers(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Blocker = request.env['task.forge.blocker'].sudo()
            role = employee._get_task_forge_role()

            if role == 'admin':
                domain = [('state', '=', 'escalated')]
            elif role == 'pl':
                team_ids = employee._get_team_employee_ids()
                domain = [
                    ('employee_id', 'in', team_ids),
                    ('state', 'in', ['ack', 'escalated']),
                ]
            elif role in ('qr', 'ql'):
                domain = [('qr_id', '=', employee.id)]
            else:
                domain = [('employee_id', '=', employee.id)]

            blockers = Blocker.search(domain, order='create_date desc', limit=200)
            data = [self._format_blocker(b) for b in blockers]
            return return_Response(message="Blockers list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/blockers/qr_action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'blocker_id': {'type': 'int', 'required': True},
        'action': {'type': 'string', 'required': True},
        'notes': {'type': 'string', 'required': False},
    })
    def qr_action(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_quality_reviewer'):
                return return_Response(message="QR role required", status=403)

            Blocker = request.env['task.forge.blocker'].sudo()
            blocker = Blocker.browse(jdata['blocker_id'])
            if not blocker.exists():
                return return_Response(message="Blocker not found", status=404)

            action = jdata['action']
            notes = jdata.get('notes')

            if action == 'no_issue':
                blocker.action_qr_no_issue(notes=notes)
                return return_Response(message="Blocker marked as No Issue", status=200, data={'data': self._format_blocker(blocker)})
            elif action == 'escalate':
                video_url = None
                image_url = None

                video_file = request.httprequest.files.get('video')
                if video_file:
                    import base64
                    vid_data = base64.b64encode(video_file.read())
                    video_url = generate_s3_link(vid_data, prefix='taskforge/blocker_videos', uid=user.employee_id.id)

                image_file = request.httprequest.files.get('image')
                if image_file:
                    import base64
                    img_data = base64.b64encode(image_file.read())
                    image_url = generate_s3_link(img_data, prefix='taskforge/blocker_images', uid=user.employee_id.id)

                blocker.action_qr_escalate(notes=notes, video_url=video_url, image_url=image_url)
                return return_Response(message="Blocker escalated to PL", status=200, data={'data': self._format_blocker(blocker)})
            else:
                return return_Response(message="Invalid action. Use 'no_issue' or 'escalate'.", status=400)
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/blockers/pl_validate', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'blocker_id': {'type': 'int', 'required': True},
        'bug_title': {'type': 'string', 'required': True},
        'bug_description': {'type': 'string', 'required': False},
        'steps_to_reproduce': {'type': 'string', 'required': False},
        'pages_affected': {'type': 'string', 'required': False},
        'impact': {'type': 'string', 'required': False},
        'impact_details': {'type': 'string', 'required': False},
    })
    def pl_validate(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_project_lead'):
                return return_Response(message="PL role required", status=403)

            Blocker = request.env['task.forge.blocker'].sudo()
            blocker = Blocker.browse(jdata['blocker_id'])
            if not blocker.exists():
                return return_Response(message="Blocker not found", status=404)

            bug = blocker.action_pl_validate(jdata)

            return return_Response(
                message="Bug validated",
                status=200,
                data={'data': {
                    'blocker': self._format_blocker(blocker),
                    'validated_bug': {
                        'id': bug.id,
                        'name': bug.name,
                        'impact': bug.impact,
                        'state': bug.state,
                    }
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    def _format_blocker(self, b):
        return {
            'id': b.id,
            'name': b.name,
            'task_id': b.task_id.id,
            'task_name': b.task_id.name,
            'project_id': b.project_id.id if b.project_id else None,
            'project_name': b.project_id.name if b.project_id else None,
            'employee_id': b.employee_id.id,
            'employee_name': b.employee_id.name,
            'qr_id': b.qr_id.id if b.qr_id else None,
            'qr_name': b.qr_id.name if b.qr_id else None,
            'pl_id': b.pl_id.id if b.pl_id else None,
            'pl_name': b.pl_id.name if b.pl_id else None,
            'blocker_reason': b.blocker_reason or '',
            'state': b.state,
            'qr_notes': b.qr_notes or '',
            'qr_video_url': b.qr_video_url or '',
            'qr_image_url': b.qr_image_url or '',
            'qr_action_at': b.qr_action_at.isoformat() if b.qr_action_at else None,
            'pl_validated_at': b.pl_validated_at.isoformat() if b.pl_validated_at else None,
            'validated_bug_id': b.validated_bug_id.id if b.validated_bug_id else None,
            'created_at': b.create_date.isoformat() if b.create_date else '',
        }
