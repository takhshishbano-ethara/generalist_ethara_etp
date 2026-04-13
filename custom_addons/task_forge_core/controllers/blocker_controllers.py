from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
import json


class TaskForgeBlockerController(http.Controller):

    @http.route('/api/v2/taskforge/create_blocker_record', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({"name": {"type": "str", "required": True}, "task_id": {"type": "int", "required": True}, "blocker_reason": {"type": "str", "required": True}, "blocker_type": {"type": "str", "required": True}})
    def create_blocker_record(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Blocker = request.env['task.forge.blocker'].sudo()
            role = employee._get_task_forge_role()

            if not role == 'tasker':
                return return_Response(message="Only Tasker Can Create the Blocker", status=404)

            blocker = Blocker.create({
                'name': jdata.get('name'),
                'task_id': jdata.get('task_id'),
                'blocker_reason': jdata.get('blocker_reason'),
                'blocker_type': jdata.get('blocker_type'),
                'priority': jdata.get('priority'),
                'employee_id': employee.id if employee else False,
                'qr_id': employee.task_forge_qr_id.id if employee.task_forge_qr_id else False,
                'pl_id': employee.task_forge_pl_id.id if employee.task_forge_pl_id else False
            })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'New Blocker Created',
                    'message': f'{employee.name} raised a blocker: "{blocker.name}".',
                    'user_id': request.env.user.id,
                    'priority': '2',
                    'res_model': 'task.forge.blocker',
                    'res_id': blocker.id,
                })
            except Exception:
                pass

            return return_Response(message="Blockers list", status=200, data={'data': self._format_blocker(blocker)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

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
                # domain = [('state', '=', 'escalated')]
                domain = []
                if kwargs.get('active_blocker') in [1, '1']:
                    domain = [('state', 'not in', ['no_issue'])]

            elif role == 'pl':
                team_ids = employee._get_team_employee_ids()
                domain = [
                    ('employee_id', 'in', team_ids),
                    ('state', 'not in', ['no_issue']),
                    # ('state', 'in', ['ack', 'escalated']),
                ]

            elif role in ('qr', 'ql'):
                domain = [('qr_id', '=', employee.id)]
                if kwargs.get('active_blocker') in [1, '1']:
                    domain.append(('state', 'not in', ['no_issue']))

            else:
                domain = [('employee_id', '=', employee.id)]
                if kwargs.get('active_blocker') in [1, '1']:
                    domain.append(('state', 'not in', ['no_issue']))

            if kwargs.get('project_id'):
                domain.append(('project_id', '=', int(kwargs.get('project_id'))))

            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = request.env['task.forge.blocker'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0
            blockers = Blocker.search(domain, order='create_date desc', limit=limit, offset=offset)
            data = [self._format_blocker(b) for b in blockers]
            return return_Response(message="Blockers list", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/blockers/qr_action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def qr_action(self, **kwargs):
        try:
            user = request.env.user
            if not user.has_group('etp_user_roles.group_quality_reviewer') and not user.user_role.id in [
                request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id,
                request.env.ref('api_auth_gateway.role_qc_non_stem').id] and not user.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id]:

                return return_Response(message="QR Or PL role required", status=403)

            Blocker = request.env['task.forge.blocker'].sudo()
            blocker = Blocker.browse(int(kwargs.get('blocker_id')))
            if not blocker.exists():
                return return_Response(message="Blocker not found", status=404)

            action = kwargs.get('action')
            notes = kwargs.get('notes')

            if action == 'no_issue':
                blocker.action_qr_no_issue(notes=notes)

                try:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'Blocker Marked No Issue',
                        'message': f'Blocker "{blocker.name}" marked as No Issue.',
                        'user_id': request.env.user.id,
                        'priority': '1',
                        'res_model': 'task.forge.blocker',
                        'res_id': blocker.id,
                    })
                except Exception:
                    pass

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

                try:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'Blocker Escalated',
                        'message': f'Blocker "{blocker.name}" has been escalated to PL.',
                        'user_id': request.env.user.id,
                        'priority': '2',
                        'res_model': 'task.forge.blocker',
                        'res_id': blocker.id,
                    })
                except Exception:
                    pass

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

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Bug Validated',
                    'message': f'Blocker "{blocker.name}" validated as bug "{bug.name}" by PL.',
                    'user_id': request.env.user.id,
                    'priority': '2',
                    'res_model': 'task.forge.validated.bug',
                    'res_id': bug.id,
                })
            except Exception:
                pass

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
            'id': b.id if b.id else 0,
            'name': b.name if b.name else "",
            'task_id': b.task_id.id if b.task_id.id else 0,
            'task_name': b.task_id.name if b.task_id.name else "",
            'project_id': b.project_id.id if b.project_id else 0,
            'project_name': b.project_id.name if b.project_id.name else "",
            'employee_id': b.employee_id.id if b.employee_id.id else 0,
            'employee_name': b.employee_id.name if b.employee_id.name else "",
            'qr_id': b.qr_id.id if b.qr_id else 0,
            'qr_name': b.qr_id.name if b.qr_id else "",
            'pl_id': b.pl_id.id if b.pl_id else 0,
            'pl_name': b.pl_id.name if b.pl_id else "",
            'blocker_reason': b.blocker_reason or '',
            'priority': b.priority or '',
            'state': b.state or "",
            'qr_notes': b.qr_notes or '',
            'qr_video_url': b.qr_video_url or '',
            'qr_image_url': b.qr_image_url or '',
            'qr_action_at': b.qr_action_at.isoformat() if b.qr_action_at else "",
            'pl_validated_at': b.pl_validated_at.isoformat() if b.pl_validated_at else "",
            'validated_bug_id': b.validated_bug_id.id if b.validated_bug_id else 0,
            'created_at': b.create_date.isoformat() if b.create_date else '',
        }
