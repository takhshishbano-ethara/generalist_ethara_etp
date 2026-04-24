from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request, generate_s3_link
)
import json
import base64
from datetime import datetime


class TaskForgeBlockerController(http.Controller):

    # ──────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _upload_files(self, file_key='image', prefix='blocker_images'):
        """Upload a single file from the request to S3, return URL."""
        image_file = request.httprequest.files.get(file_key)
        if not image_file:
            return ''
        file_content = image_file.read()
        if not file_content:
            return ''
        file_b64 = base64.b64encode(file_content).decode('utf-8')
        return generate_s3_link(file_b64, prefix=prefix, filename=image_file.filename) or ''

    def _upload_multiple_files(self, prefix='blocker_documents'):
        """Upload multiple documents from request, return list of URLs."""
        urls = []
        files = request.httprequest.files.getlist('documents')
        for f in files:
            content = f.read()
            if content:
                b64 = base64.b64encode(content).decode('utf-8')
                url = generate_s3_link(b64, prefix=prefix, filename=f.filename)
                if url:
                    urls.append(url)
        return urls

    def _format_blocker(self, b):
        escalation_logs = []
        for log in b.escalation_log_ids:
            escalation_logs.append({
                'id': log.id,
                'from_role': log.from_role or '',
                'to_role': log.to_role or '',
                'action': log.action or '',
                'notes': log.notes or '',
                'image_url': log.image_url or '',
                'document_urls': log.document_urls.split(',') if log.document_urls else [],
                'action_by_id': log.action_by_id.id if log.action_by_id else 0,
                'action_by_name': log.action_by_name or '',
                'created_at': log.create_date.isoformat() if log.create_date else '',
            })

        return {
            'steps_to_reproduce': b.steps_to_reproduce or '',
            'task_page_affected': b.affected_area or '',
            'id': b.id if b.id else 0,
            'name': b.name if b.name else "",
            'task_id': b.task_id.id if b.task_id else 0,
            'task_name': b.task_id.name if b.task_id and b.task_id.name else "",
            'project_id': b.project_id.id if b.project_id else 0,
            'project_name': b.project_id.name if b.project_id and b.project_id.name else "",
            'employee_id': b.employee_id.id if b.employee_id else 0,
            'employee_name': b.employee_id.name if b.employee_id and b.employee_id.name else "",
            'qr_id': b.qr_id.id if b.qr_id else 0,
            'qr_name': b.qr_id.name if b.qr_id and b.qr_id.name else "",
            'pl_id': b.pl_id.id if b.pl_id else 0,
            'pl_name': b.pl_id.name if b.pl_id and b.pl_id.name else "",
            'blocker_reason': b.blocker_reason or '',
            'blocker_type': b.blocker_type or '',
            'priority': b.priority or '',
            'blocker_issue_id': b.blocker_issue_id.id if b.blocker_issue_id else 0,
            'blocker_issue': b.blocker_issue_id.name if b.blocker_issue_id and b.blocker_issue_id.name else '',
            'state': b.state or "",
            'escalation_level': b.escalation_level or 'qr',
            'blocker_image_url': b.blocker_image_url or '',
            # QR
            'qr_notes': b.qr_notes or '',
            'qr_video_url': b.qr_video_url or '',
            'qr_image_url': b.qr_image_url or '',
            'qr_action_at': b.qr_action_at.isoformat() if b.qr_action_at else "",
            # PL
            'pl_notes': b.pl_notes or '',
            'pl_image_url': b.pl_image_url or '',
            'pl_action_at': b.pl_action_at.isoformat() if b.pl_action_at else "",
            'pl_validated_at': b.pl_validated_at.isoformat() if b.pl_validated_at else "",
            # CTO
            'cto_notes': b.cto_notes or '',
            'cto_image_url': b.cto_image_url or '',
            'cto_action_at': b.cto_action_at.isoformat() if b.cto_action_at else "",
            # Resolution
            'resolved_by_id': b.resolved_by_id.id if b.resolved_by_id else 0,
            'resolved_by_name': b.resolved_by_id.name if b.resolved_by_id else '',
            'resolved_at': b.resolved_at.isoformat() if b.resolved_at else '',
            'resolution_notes': b.resolution_notes or '',
            # Bug
            'validated_bug_id': b.validated_bug_id.id if b.validated_bug_id else 0,
            'created_at': b.create_date.isoformat() if b.create_date else '',
            # Escalation history
            'escalation_logs': escalation_logs,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Create Blocker (Tasker)
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/create_blocker_record', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def create_blocker_record(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            Blocker = request.env['task.forge.blocker'].sudo()
            role = employee._get_task_forge_role()

            if role != 'tasker':
                return return_Response(message="Only Tasker Can Create the Blocker", status=404)

            blocker_dict = {
                'name': kwargs.get('name'),
                'blocker_reason': kwargs.get('blocker_reason'),
                'blocker_type': kwargs.get('blocker_type'),
                'priority': kwargs.get('priority'),
                'employee_id': employee.id,
                'qr_id': employee.task_forge_qr_id.id if employee.task_forge_qr_id else False,
                'pl_id': employee.task_forge_pl_id.id if employee.task_forge_pl_id else False,
                'escalation_level': 'qr'
            }

            if kwargs.get('blocker_issue_id'):
                blocker_dict['blocker_issue_id'] = int(kwargs.get('blocker_issue_id'))

            if kwargs.get('task_id'):
                task = request.env['task.forge.log'].sudo().browse(int(kwargs.get('task_id')))
                if not task.exists():
                    return return_Response(message="Task not found", status=404)
                blocker_dict['task_id'] = task.id
                now = datetime.now()
                pause_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
                task.write({
                    'state': 'blocker',
                    'pause_time': kwargs.get('pause_time') or pause_time_str,
                })

            blocker_image_url = self._upload_files('image', 'blocker_images')
            if blocker_image_url:
                blocker_dict['blocker_image_url'] = blocker_image_url

            document_urls = self._upload_multiple_files('blocker_documents')

            blocker = Blocker.create(blocker_dict)

            blocker._log_escalation('tasker', 'qr', 'create',
                                    notes=kwargs.get('blocker_reason') or '',
                                    image_url=blocker_image_url,
                                    document_urls=document_urls)


            try:
                if blocker.qr_id and blocker.qr_id.user_id:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'New Blocker Created',
                        'message': '%s raised a blocker: "%s".' % (employee.name, blocker.name),
                        'user_id': blocker.qr_id.user_id.id,
                        'priority': '2',
                        'res_model': 'task.forge.blocker',
                        'res_id': blocker.id,
                        'project_id': blocker.project_id.id if blocker.project_id else False,
                    })
            except Exception:
                pass

            return return_Response(message="Blocker created", status=200, data={'data': self._format_blocker(blocker)})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. List Blockers
    # ──────────────────────────────────────────────────────────────────────────

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
                domain = []
                if kwargs.get('active_blocker') in [1, '1']:
                    domain = [('state', 'not in', ['no_issue', 'resolved'])]
            elif role == 'pl':
                team_ids = employee._get_team_employee_ids()
                # domain = ['|', ('employee_id', 'in', team_ids), ('state', '=', 'escalated_to_pl')]
                domain = [('employee_id', 'in', team_ids)]
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
            if kwargs.get('status'):
                domain.append(('state', 'in', [kwargs.get('status')]))
            if kwargs.get('priority'):
                domain.append(('priority', '=', kwargs.get('priority')))
            if kwargs.get('employee_id'):
                domain.append(('employee_id', '=', int(kwargs.get('employee_id'))))
            if kwargs.get('assignee'):
                domain.append('|')
                domain.append(('qr_id', '=', int(kwargs.get('assignee'))))
                domain.append(('pl_id', '=', int(kwargs.get('assignee'))))

            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = Blocker.search_count(domain)
            if not kwargs.get('page'):
                limit = total_count or 1
                offset = 0
            blockers = Blocker.search(domain, order='create_date desc', limit=limit, offset=offset)
            data = [self._format_blocker(b) for b in blockers]
            return return_Response(message="Blockers list", status=200, data={'data': data, 'total_record_count': total_count})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Blocker Action (unified — QR / PL / CTO)
    #    Params: blocker_id, action, notes, image (file), video (file), documents (files)
    #    Bug params (only for validate_bug): bug_title, bug_description, steps_to_reproduce,
    #                                         pages_affected, impact, impact_details
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/taskforge/blockers/action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def blocker_action(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            role = employee._get_task_forge_role()
            if role == 'tasker':
                return return_Response(message="Taskers cannot take action on blockers", status=403)

            Blocker = request.env['task.forge.blocker'].sudo()
            blocker = Blocker.browse(int(kwargs.get('blocker_id')))
            if not blocker.exists():
                return return_Response(message="Blocker not found", status=404)

            action = kwargs.get('action')
            notes = kwargs.get('notes')
            image_url = self._upload_files('image', 'taskforge/blocker_images')
            video_url = self._upload_files('video', 'taskforge/blocker_videos')
            document_urls = self._upload_multiple_files('taskforge/blocker_documents')

            valid_actions = {
                # (blocker_state, caller_role): [allowed_actions]
                ('pending', 'qr'): ['no_issue', 'escalate'],
                ('pending', 'ql'): ['no_issue', 'escalate'],
                ('pending', 'pl'): ['no_issue', 'escalate', 'resolve'],
                ('pending', 'admin'): ['no_issue', 'escalate', 'resolve'],
                ('escalated_to_pl', 'pl'): ['resolve', 'escalate'],
                ('escalated_to_pl', 'admin'): ['resolve', 'escalate', 'validate_bug'],
                ('ack', 'pl'): ['resolve', 'escalate'],
                ('ack', 'admin'): ['resolve', 'escalate', 'validate_bug'],
                ('escalated_to_cto', 'admin'): ['resolve', 'validate_bug'],
            }

            allowed = valid_actions.get((blocker.state, role), [])
            if not allowed:
                return return_Response(
                    message="You cannot act on this blocker in its current state (%s). Your role: %s" % (blocker.state, role),
                    status=403)
            if action not in allowed:
                return return_Response(
                    message="Invalid action '%s'. Allowed actions for %s at state '%s': %s" % (action, role, blocker.state, ', '.join(allowed)),
                    status=400)

            if action == 'no_issue':
                blocker.action_qr_no_issue(notes=notes)
                return return_Response(message="Blocker marked as No Issue", status=200, data={'data': self._format_blocker(blocker)})

            elif action == 'escalate':
                current_level = blocker.escalation_level or 'qr'
                if current_level == 'qr':
                    blocker.action_qr_escalate(notes=notes, video_url=video_url, image_url=image_url, document_urls=document_urls)

                    if kwargs.get('priority'):
                        blocker.priority = kwargs.get('priority')
                    return return_Response(message="Blocker escalated to PL", status=200, data={'data': self._format_blocker(blocker)})
                elif current_level == 'pl':
                    blocker.action_pl_escalate_to_cto(notes=notes, image_url=image_url, document_urls=document_urls)
                    blocker.sudo().write({
                        'steps_to_reproduce': kwargs.get('steps_to_reproduce') or blocker.steps_to_reproduce,
                        'affected_area': kwargs.get('task_page_affected') or blocker.affected_area,
                        'priority': kwargs.get('priority') or blocker.priority,
                    })

                    return return_Response(message="Blocker escalated to CTO", status=200, data={'data': self._format_blocker(blocker)})
                else:
                    return return_Response(message="Blocker is already at highest escalation level (CTO)", status=400)

            elif action == 'resolve':
                current_level = blocker.escalation_level or 'qr'
                if current_level in ('qr', 'pl') and role in ('pl', 'admin'):
                    blocker.action_pl_resolve(notes=notes, image_url=image_url, document_urls=document_urls)
                elif current_level == 'cto' and role == 'admin':
                    blocker.action_cto_resolve(notes=notes, image_url=image_url, document_urls=document_urls)
                else:
                    return return_Response(message="Cannot resolve at this level with your role", status=403)
                return return_Response(message="Blocker resolved", status=200, data={'data': self._format_blocker(blocker)})

            elif action == 'validate_bug':
                bug_data = {
                    'bug_title': kwargs.get('bug_title', blocker.name),
                    'bug_description': kwargs.get('bug_description', ''),
                    'steps_to_reproduce': kwargs.get('steps_to_reproduce', ''),
                    'pages_affected': kwargs.get('pages_affected', ''),
                    'impact': kwargs.get('impact', 'medium'),
                    'impact_details': kwargs.get('impact_details', ''),
                }
                bug = blocker.action_cto_validate_bug(bug_data, notes=notes, image_url=image_url, document_urls=document_urls)
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
            else:
                return return_Response(message="Unknown action: %s" % action, status=400)
        except Exception as e:
            return return_Response(message=str(e), status=400)

    # Legacy endpoints kept for backward compatibility — redirect to unified action
    @http.route('/api/v2/taskforge/blockers/qr_action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def qr_action(self, **kwargs):
        return self.blocker_action(**kwargs)

    @http.route('/api/v2/taskforge/blockers/pl_action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def pl_action(self, **kwargs):
        return self.blocker_action(**kwargs)

    @http.route('/api/v2/taskforge/blockers/cto_action', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def cto_action(self, **kwargs):
        return self.blocker_action(**kwargs)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. PL Validate (legacy — kept for backward compatibility)
    # ──────────────────────────────────────────────────────────────────────────

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

            bug = blocker.action_cto_validate_bug(jdata)
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

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Blocker Assignee List
    # ──────────────────────────────────────────────────────────────────────────

    @http.route('/api/v2/get_blocker_assignee_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({})
    def get_blocker_assignee_list(self, **kwargs):
        temp = []
        try:
            if kwargs.get('project_id'):
                project_id = int(kwargs.get('project_id'))
                projects = request.env['project.project'].sudo().browse(project_id)
                if not projects.exists():
                    return return_Response(message="Project not found", status=404)
            else:
                projects = request.env['project.project'].sudo().search([])
                if not projects:
                    return return_Response(message="Project not found", status=404)
            for project in projects:
                for emp in project.project_lead | project.project_qc_reviewer:
                    temp.append({
                        'id': emp.id,
                        'name': emp.name
                    })
            return return_Response(
                message="success",
                status=200,
                data={'data': temp}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/blockers/stats', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def blocker_stats(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id
            if not employee:
                return return_Response(message="Employee profile not found", status=404)

            role = employee._get_task_forge_role()
            team_ids = employee._get_team_employee_ids()
            Blocker = request.env['task.forge.blocker'].sudo()

            if role == 'admin':
                base_domain = []
            elif role == 'pl':
                base_domain = [('employee_id', 'in', team_ids)]
            elif role in ('qr', 'ql'):
                base_domain = [('qr_id', '=', employee.id)]
            else:
                base_domain = [('employee_id', '=', employee.id)]

            if kwargs.get('project_id'):
                base_domain.append(('project_id', '=', int(kwargs.get('project_id'))))

            total = Blocker.search_count(base_domain)
            pending = Blocker.search_count(base_domain + [('state', '=', 'pending')])
            escalated_to_pl = Blocker.search_count(base_domain + [('state', '=', 'escalated_to_pl')])
            escalated_to_cto = Blocker.search_count(base_domain + [('state', '=', 'escalated_to_cto')])
            resolved = Blocker.search_count(base_domain + [('state', '=', 'resolved')])
            no_issue = Blocker.search_count(base_domain + [('state', '=', 'no_issue')])
            validated = Blocker.search_count(base_domain + [('state', '=', 'validated')])

            return return_Response(
                message="Blocker stats",
                status=200,
                data={
                    'total': total,
                    'pending': pending,
                    'escalated_to_pl': escalated_to_pl,
                    'escalated_to_cto': escalated_to_cto,
                    'resolved': resolved,
                    'no_issue': no_issue,
                    'validated': validated,
                }
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)


    @http.route('/api/v2/get_blocker_issues_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_blocker_issues_list(self, **kwargs):
        temp = []
        try:
            blocker_issues = request.env['res.blocker.issues'].sudo().search([])
            for bi in blocker_issues:
                temp.append({
                    'id': bi.id,
                    'name': bi.name
                })
            return return_Response(
                message="success",
                status=200,
                data={'data': temp}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)


