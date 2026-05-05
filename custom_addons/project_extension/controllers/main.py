from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value
import base64

def create_calendar_event(request, meet_body):
    meet_vals = {
        "name": meet_body.get("subject"),
        "start": meet_body.get("start_datetime"),
        "stop": meet_body.get("stop_datetime"),
        # "duration": duration_hours,
        "description": meet_body.get("description"),
        "privacy": "public",
        "show_as": "busy",
        "is_google_meet": True,
        "partner_ids": [(6, 0, meet_body.get("partner_ids"))],
    }
    event_id = request.env['calendar.event'].sudo().create(meet_vals)
    return event_id

class ProjectController(http.Controller):

    @validate_token
    @http.route('/api/v1/get_employee_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_employee_list(self, **kwargs):
        temp = []
        try:
            jdata = kwargs.get('jdata')
            page = int(jdata.get('page')) if jdata.get('page') else 1
            limit = int(jdata.get('limit')) if jdata.get('limit') else 1
            offset = (page - 1) * limit
            domain = []
            if jdata.get('designation_id'):
                domain = [('designation_id', '=', int(jdata.get('designation_id')))]
            if jdata.get('designation'):
                if jdata.get('designation') == 'se':
                    domain = [('designation_id', '=', request.env.ref('project_extension.designation_software_engineer').id)]
                elif jdata.get('designation') == 'aire':
                    domain = [('designation_id', '=', request.env.ref('project_extension.designation_ai_research_engineer').id)]
                elif jdata.get('designation') == 'pl':
                    domain = [('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_pl_technical').id, request.env.ref('api_auth_gateway.role_pl_stem').id, request.env.ref('api_auth_gateway.role_pl_non_stem').id])]
                elif jdata.get('designation') == 'qc_review':
                    domain = [('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_qc_technical').id, request.env.ref('api_auth_gateway.role_qc_stem').id, request.env.ref('api_auth_gateway.role_qc_non_stem').id])]
                elif jdata.get('designation') == 'tasker':
                    domain = [('user_id.user_role', 'in', [request.env.ref('api_auth_gateway.role_tasker_technical').id, request.env.ref('api_auth_gateway.role_tasker_stem').id, request.env.ref('api_auth_gateway.role_tasker_non_stem').id])]
                else:
                    domain = [('designation_id.name', 'ilike', jdata.get('designation'))]
            if jdata.get('search'):
                domain.append(('name', 'ilike', jdata.get('search')))
            employee_count = request.env['hr.employee'].sudo().search_count(domain)
            if not jdata.get('page'):
                limit = employee_count
                offset = 0
            employee_list = request.env['hr.employee'].sudo().search(domain, order='id DESC', limit=limit, offset=offset)
            for emp in employee_list:
                temp.append({
                    'id': safe_get_value(emp, 'id', 'int'),
                    'name': safe_get_value(emp, 'name', 'str'),
                    'mobile': safe_get_value(emp, 'work_phone', 'str'),
                    'whatsapp_number': safe_get_value(emp, 'whatsapp_number', 'str'),
                    'email': safe_get_value(emp, 'work_email', 'str'),
                })
            return return_Response(message="Success", status=200, data={"record": temp, "total_record_count": employee_count, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/add_whatsapp_members', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({"name": {"type": "str", "required": True}, "email": {"type": "email", "required": True}, "mobile": {"type": "mobile", "required": True}})
    def add_whatsapp_members(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            created_record = request.env['whatsapp.group.members'].sudo().create({
                'name': jdata.get('name'),
                'email': jdata.get('email'),
                'country_code': jdata.get('country_code') or "+91",
                'phone_number': jdata.get('mobile'),
            })
            return return_Response(message="Success", status=200, data={"record": {
                    'id': safe_get_value(created_record, 'id', 'int'),
                    'name': safe_get_value(created_record, 'name', 'str'),
                    'email': safe_get_value(created_record, 'email', 'str'),
                    'mobile': safe_get_value(created_record, 'phone_number', 'str'),
                    'country_code': safe_get_value(created_record, 'country_code', 'str')
                }})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_whatsapp_member_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_whatsapp_member_list(self, **kwargs):
        temp = []
        try:
            jdata = kwargs.get('jdata')
            page = int(jdata.get('page')) if jdata.get('page') else 1
            limit = int(jdata.get('limit')) if jdata.get('limit') else 1
            offset = (page - 1) * limit
            domain = []
            if jdata.get('search'):
                domain.append(('|'))
                domain.append(('|'))
                domain.append(('name', 'ilike', jdata.get('search')))
                domain.append(('email', 'ilike', jdata.get('search')))
                domain.append(('phone_number', 'ilike', jdata.get('search')))
            whatsapp_member_count = request.env['whatsapp.group.members'].sudo().search_count(domain)
            if not jdata.get('page'):
                limit = whatsapp_member_count
                offset = 0
            whatsapp_member_list = request.env['whatsapp.group.members'].sudo().search(domain, order='id DESC', limit=limit, offset=offset)
            for wml in whatsapp_member_list:
                temp.append({
                    'id': safe_get_value(wml, 'id', 'int'),
                    'name': safe_get_value(wml, 'name', 'str'),
                    'email': safe_get_value(wml, 'email', 'str'),
                    'mobile': safe_get_value(wml, 'phone_number', 'str'),
                    'country_code': safe_get_value(wml, 'country_code', 'str')
                })
            return return_Response(message="Success", status=200, data={"record": temp, "total_record_count": whatsapp_member_count, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/create_project_record', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    def create_project_record(self, **kwargs):
        try:
            def parse_ids(key):
                val = kwargs.get(key)
                if not val: return []
                if isinstance(val, str):
                    my_list = [int(x) for x in val.strip('[]').split(',') if x.strip()]
                    return my_list
                return val if isinstance(val, list) else [val]

            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if not user_id.employee_id:
                return return_Response(message="Employee not found", status=404)
            project_category = "non_stem"
            y_project_type = 'non-stem'
            if user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_technical').id]:
                project_category = 'technical'
                y_project_type = 'technical'
            if user_id.user_role.id in [request.env.ref('api_auth_gateway.role_pl_stem').id]:
                project_category = 'stem'
                y_project_type = 'stem'

            vals = {
                "name": kwargs.get("name") or kwargs.get("internal_project_name"),
                "internal_project_name": kwargs.get("internal_project_name") or kwargs.get("name"),
                "client_name": kwargs.get("client_name") or kwargs.get("internal_client_name"),
                "project_category": kwargs.get("project_category") or project_category,
                "project_type": kwargs.get('project_type'),
                "sample_task_number": int(kwargs.get("sample_task_number", 0)),
                "project_lead": [(6, 0, parse_ids("project_lead"))],
                "project_aire": [(6, 0, parse_ids("project_aire"))],
                "project_swe": [(6, 0, parse_ids("project_swe"))],
                # "whatsapp_group_members": [(6, 0, parse_ids('whatsapp_group_members'))],
                "kick_off_to_mails": kwargs.get('kick_off_to_mails'),
                "kick_off_subject": kwargs.get('kick_off_subject'),
                "kick_off_body": kwargs.get('kick_off_body'),
                "ai_generated_description": kwargs.get("ai_generated_description"),
                "internal_client_name": kwargs.get("internal_client_name") or kwargs.get("client_name"),
                "date_start": kwargs.get("date_start"),
                "date": kwargs.get("date_end"),
                'description': kwargs.get("description"),
                "whatsapp_group_name": kwargs.get('whatsapp_group_name'),
                'slack_channel_name': kwargs.get('slack_channel_name'),
                'y_project_type': kwargs.get('y_project_type') or y_project_type,
                "slack_members": [(6, 0, parse_ids("slack_members"))],
                'non_stemp_project_status': 'not_started',
            }

            if kwargs.get('whatsapp_group_members'):
                import json
                if isinstance(kwargs.get('whatsapp_group_members'), str):
                    wgm_data = json.loads(kwargs.get('whatsapp_group_members'))
                else:
                    wgm_data = kwargs.get('whatsapp_group_members')
                wgm_list = []
                for rec in wgm_data:
                    whatsapp_gm = request.env['whatsapp.group.members'].sudo().search([('phone_number', '=', rec.get('mobile'))], limit=1)
                    if not whatsapp_gm:
                        whatsapp_gm = request.env['whatsapp.group.members'].sudo().create({
                            'name': rec.get('name'),
                            'email': rec.get('email'),
                            'country_code': "+91",
                            'phone_number': rec.get('mobile'),
                        })
                    wgm_list.append(whatsapp_gm.id)
                if wgm_list:
                    vals['whatsapp_group_members'] = [(6, 0, wgm_list)]

            stage_xml = 'project_extension.project_project_stage_ethara_14' if kwargs.get(
                'save_as_draft') == '1' else 'project_extension.project_project_stage_ethara_4'
            vals['stage_id'] = request.env.ref(stage_xml).id
            vals['non_stemp_project_status'] = "draft" if kwargs.get('save_as_draft') == '1' else "not_started"

            if kwargs.get('is_rubrics_required') in ['1', 1, True, 'true']:
                vals['is_rubrics_required'] = True
            if kwargs.get('is_justification_required') in ['1', 1, True, 'true']:
                vals['is_justification_required'] = True
            if kwargs.get('is_response_required') in ['1', 1, True, 'true']:
                vals['is_response_required'] = True
                no_of_responses = int(kwargs.get('no_of_responses', 0))
                vals['no_of_responses'] = no_of_responses

            attachment_ids = []
            files = request.httprequest.files.getlist('files')

            for file in files:
                file_content = file.read()
                if not file_content:
                    continue
                attachment = request.env['ir.attachment'].sudo().create({
                    'name': file.filename,
                    'datas': base64.b64encode(file_content),
                    'res_model': 'project.project',
                    'type': 'binary',
                    'mimetype': file.content_type
                })
                attachment_ids.append(attachment.id)

            if attachment_ids:
                vals['project_attachments'] = [(6, 0, attachment_ids)]
            # '''''''''''''''''''''''''''''''''''''''
            if kwargs.get('project_qc_reviewer'):
                vals['project_qc_reviewer'] = [(6, 0, parse_ids("project_qc_reviewer"))]
            if kwargs.get('project_tasker'):
                vals['project_tasker'] = [(6, 0, parse_ids("project_tasker"))]

            # Schedule Meeting
            if kwargs.get('meeting_date'):
                vals['meeting_date'] = kwargs.get('meeting_date')
            if kwargs.get('meeting_link'):
                vals['meeting_link'] = kwargs.get('meeting_link')
            if kwargs.get('meeting_agenda'):
                vals['meeting_agenda'] = kwargs.get('meeting_agenda')
            if kwargs.get('meeting_to_mails'):
                vals['meeting_to_mails'] = kwargs.get('meeting_to_mails')
            if kwargs.get('meeting_cc_mails'):
                vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
            if kwargs.get('meeting_bcc_mails'):
                vals['meeting_bcc_mails'] = kwargs.get('meeting_bcc_mails')
            if kwargs.get('meeting_subject'):
                vals['meeting_subject'] = kwargs.get('meeting_subject')
            if kwargs.get('meeting_body'):
                vals['meeting_body'] = kwargs.get('meeting_body')
            if kwargs.get('meeting_cc_mails'):
                vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
            meeting_attachments = request.httprequest.files.getlist('meeting_attachments')
            if meeting_attachments:
                attachment_ids = []
                for file in meeting_attachments:
                    file_content = file.read()
                    if not file_content: continue

                    attachment = request.env['ir.attachment'].sudo().create({
                        'name': file.filename,
                        'datas': base64.b64encode(file_content),
                        'res_model': 'project.project',
                        'type': 'binary',
                        'mimetype': file.content_type
                    })
                    attachment_ids.append(attachment.id)
                if attachment_ids:
                    vals['meeting_attachments'] = [(6, 0, attachment_ids)]
            project = request.env['project.project'].sudo().create(vals)

            if kwargs.get('rubric_categories'):
                import json as _json
                raw = kwargs.get('rubric_categories')
                if isinstance(raw, str):
                    raw = _json.loads(raw)
                if isinstance(raw, list):
                    for cat_data in raw:
                        cat = request.env['rubric.category'].sudo().create({
                            'name': cat_data.get('name', ''),
                            'project_id': project.id,
                            'sequence': cat_data.get('sequence', 10),
                        })
                        for opt in cat_data.get('options', []):
                            request.env['rubric.category.option'].sudo().create({
                                'name': opt.get('name', ''),
                                'value': int(opt.get('value', 0)),
                                'sequence': opt.get('sequence', 10),
                                'category_id': cat.id,
                            })
                        for dim in cat_data.get('dimensions', []):
                            request.env['rubric.dimension'].sudo().create({
                                'name': dim.get('name', ''),
                                'description': dim.get('description', ''),
                                'sequence': dim.get('sequence', 10),
                                'category_id': cat.id,
                            })

            if project.is_response_required and project.no_of_responses > 0:
                request.env['project.response.config'].sudo().generate_configs_for_project(
                    project.id, project.no_of_responses
                )

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'New Project Created',
                    'message': f'Project "{project.name}" has been created.',
                    'user_id': request.env.uid,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass
            try:
                if kwargs.get('meeting_body') and kwargs.get('meeting_to_mails'):
                    email_body = kwargs.get('meeting_body', '')
                    meeting_info_html = f"""
                                    <div style="margin-top: 30px; padding: 15px; border: 1px solid #e1e1e1; background-color: #f9f9f9; border-radius: 5px; font-family: sans-serif;">
                                        <h4 style="margin-top: 0; color: #714B67;">📅 Meeting Information</h4>
                                        <p><b>Date/Time:</b> {kwargs.get('meeting_date', 'TBD')}</p>
                                        <p><b>Agenda:</b> {kwargs.get('meeting_agenda', 'N/A')}</p>
                                        <p><b>Meeting Link:</b> <a href="{kwargs.get('meeting_link', '#')}" style="color: #008784; text-decoration: none;">{kwargs.get('meeting_link')}</a></p>
                                    </div>
                                """
                    full_body = f"{email_body}{meeting_info_html}"

                    mail_values = {
                        'subject': kwargs.get('meeting_subject', f"Meeting Invitation: {project.name}"),
                        'body_html': full_body,
                        'email_to': kwargs.get('meeting_to_mails'),
                        'email_cc': kwargs.get('meeting_cc_mails'),
                        'email_add_signature': False,
                        'email_bcc': kwargs.get('meeting_bcc_mails'),
                        'attachment_ids': [(6, 0, project.meeting_attachments.ids)] if project.meeting_attachments else [],
                        'auto_delete': False,
                    }

                    mail = request.env['mail.mail'].sudo().create(mail_values)
                    mail.send()
            except Exception as e:
                print(f"Error While Sending Mail: {e}")

            return return_Response(
                message="Project Created Successfully",
                status=200,
                data={
                    'project_id': project.id
                }
            )

        except Exception as e:
            return return_Response(message="Error occurred", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/update_project_record', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    def update_project_record(self, **kwargs):
        try:
            project_id = kwargs.get('id')
            if not project_id:
                return return_Response(message="ID is required", status=400)

            project = request.env['project.project'].sudo().browse(int(project_id))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)

            def parse_ids(key):
                val = kwargs.get(key)
                if val is None: return None
                if not val or val == '[]' or val == '': return []
                if isinstance(val, str):
                    try:
                        return [int(x) for x in val.strip('[]').split(',') if x.strip()]
                    except ValueError:
                        return []
                return val if isinstance(val, list) else [val]

            vals = {}

            # Simple Mapping (String/Text/Integer)
            field_mapping = {
                "name": "name",
                "internal_project_name": "internal_project_name",
                "client_name": "client_name",
                "project_category": "project_category",
                "project_type": "project_type",
                "sample_task_number": "sample_task_number",
                "kick_off_to_mails": "kick_off_to_mails",
                "kick_off_subject": "kick_off_subject",
                "kick_off_body": "kick_off_body",
                "ai_generated_description": "ai_generated_description",
                "internal_client_name": "internal_client_name",
                "date_start": "date_start",
                "date_end": "date",  # Odoo field is 'date'
                "description": "description",
                "whatsapp_group_name": "whatsapp_group_name",
                "y_project_type": "y_project_type",
                "slack_channel_name": "slack_channel_name",
            }

            for kwarg_key, odoo_field in field_mapping.items():
                if kwarg_key in kwargs:
                    val = kwargs.get(kwarg_key)
                    if odoo_field == 'sample_task_number':
                        vals[odoo_field] = int(val) if val else 0
                    else:
                        vals[odoo_field] = val

            m2m_fields = ["project_lead", "project_aire", "project_swe", "slack_members"]
            for field in m2m_fields:
                parsed_ids = parse_ids(field)
                if parsed_ids is not None:
                    vals[field] = [(6, 0, parsed_ids)]

            if kwargs.get('project_qc_reviewer'):
                vals['project_qc_reviewer'] = [(6, 0, parse_ids('project_qc_reviewer') or project.project_qc_reviewer.ids)]
            if kwargs.get('project_tasker'):
                vals['project_tasker'] = [(6, 0, parse_ids('project_tasker') or project.project_tasker.ids)]

            if kwargs.get('is_rubrics_required') in ['1', 1, True, 'true']:
                vals['is_rubrics_required'] = True
            elif kwargs.get('is_rubrics_required') in ['0', 0, False, 'false']:
                vals['is_rubrics_required'] = False
            if kwargs.get('is_justification_required') in ['1', 1, True, 'true']:
                vals['is_justification_required'] = True
            elif kwargs.get('is_justification_required') in ['0', 0, False, 'false']:
                vals['is_justification_required'] = False

            if kwargs.get('rubric_categories'):
                import json as _json
                raw = kwargs.get('rubric_categories')
                if isinstance(raw, str):
                    raw = _json.loads(raw)
                if isinstance(raw, list):
                    project.rubric_category_ids.unlink()
                    for cat_data in raw:
                        options = cat_data.get('options', [])
                        dimensions = cat_data.get('dimensions', [])
                        cat = request.env['rubric.category'].sudo().create({
                            'name': cat_data.get('name', ''),
                            'project_id': project.id,
                            'sequence': cat_data.get('sequence', 10),
                        })
                        for opt in options:
                            request.env['rubric.category.option'].sudo().create({
                                'name': opt.get('name', ''),
                                'value': int(opt.get('value', 0)),
                                'sequence': opt.get('sequence', 10),
                                'category_id': cat.id,
                            })
                        for dim in dimensions:
                            request.env['rubric.dimension'].sudo().create({
                                'name': dim.get('name', ''),
                                'description': dim.get('description', ''),
                                'sequence': dim.get('sequence', 10),
                                'category_id': cat.id,
                            })

            if kwargs.get('whatsapp_group_members'):
                raw_wgm = kwargs.get('whatsapp_group_members')
                wgm_data = raw_wgm
                if isinstance(raw_wgm, str):
                    try:
                        import json as _json
                        wgm_data = _json.loads(raw_wgm)
                    except (ValueError, TypeError):
                        wgm_data = []
                if isinstance(wgm_data, list) and wgm_data:
                    wgm_list = []
                    for rec in wgm_data:
                        if not isinstance(rec, dict):
                            continue
                        whatsapp_gm = request.env['whatsapp.group.members'].sudo().search([('phone_number', '=', rec.get('mobile'))], limit=1)
                        if not whatsapp_gm:
                            whatsapp_gm = request.env['whatsapp.group.members'].sudo().create({
                                'name': rec.get('name'),
                                'email': rec.get('email'),
                                'country_code': "+91",
                                'phone_number': rec.get('mobile'),
                            })
                        wgm_list.append(whatsapp_gm.id)
                    if wgm_list:
                        vals['whatsapp_group_members'] = [(6, 0, wgm_list)]

            # if project.stage_id.id == request.env.ref('project_extension.project_project_stage_ethara_14').id and not kwargs.get('stage_id'):
            #     vals['stage_id'] = request.env.ref('project_extension.project_project_stage_ethara_4').id
            #     vals['non_stemp_project_status'] = 'not_started'
            # elif kwargs.get('stage_id'):
            #     vals['stage_id'] = int(kwargs.get('stage_id'))
            if project.stage_id.id in [request.env.ref('project_extension.project_project_stage_ethara_14').id, request.env.ref('project_extension.project_project_stage_ethara_4').id]:
                stage_xml = 'project_extension.project_project_stage_ethara_14' if kwargs.get(
                    'save_as_draft') == '1' else 'project_extension.project_project_stage_ethara_4'
                vals['stage_id'] = request.env.ref(stage_xml).id
                vals['non_stemp_project_status'] = "draft" if kwargs.get('save_as_draft') == '1' else "not_started"
            elif kwargs.get('stage_id'):
                vals['stage_id'] = int(kwargs.get('stage_id'))

            files = request.httprequest.files.getlist('files')
            if files:
                attachment_ids = []
                for file in files:
                    file_content = file.read()
                    if not file_content: continue

                    attachment = request.env['ir.attachment'].sudo().create({
                        'name': file.filename,
                        'datas': base64.b64encode(file_content),
                        'res_model': 'project.project',
                        'res_id': project.id,
                        'type': 'binary',
                        'mimetype': file.content_type
                    })
                    attachment_ids.append(attachment.id)
                if attachment_ids:
                    vals['project_attachments'] = [(4, aid) for aid in attachment_ids]

                # Schedule Meeting
                if kwargs.get('meeting_date'):
                    vals['meeting_date'] = kwargs.get('meeting_date')
                if kwargs.get('meeting_link'):
                    vals['meeting_link'] = kwargs.get('meeting_link')
                if kwargs.get('meeting_agenda'):
                    vals['meeting_agenda'] = kwargs.get('meeting_agenda')
                if kwargs.get('meeting_to_mails'):
                    vals['meeting_to_mails'] = kwargs.get('meeting_to_mails')
                if kwargs.get('meeting_cc_mails'):
                    vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
                if kwargs.get('meeting_bcc_mails'):
                    vals['meeting_bcc_mails'] = kwargs.get('meeting_bcc_mails')
                if kwargs.get('meeting_subject'):
                    vals['meeting_subject'] = kwargs.get('meeting_subject')
                if kwargs.get('meeting_body'):
                    vals['meeting_body'] = kwargs.get('meeting_body')
                if kwargs.get('meeting_cc_mails'):
                    vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
                meeting_attachments = request.httprequest.files.getlist('meeting_attachments')
                if meeting_attachments:
                    attachment_ids = []
                    for file in meeting_attachments:
                        file_content = file.read()
                        if not file_content: continue

                        attachment = request.env['ir.attachment'].sudo().create({
                            'name': file.filename,
                            'datas': base64.b64encode(file_content),
                            'res_model': 'project.project',
                            'type': 'binary',
                            'mimetype': file.content_type
                        })
                        attachment_ids.append(attachment.id)
                    if attachment_ids:
                        vals['meeting_attachments'] = [(6, 0, attachment_ids)]
            if vals:
                project.sudo().write(vals)
                try:
                    request.env['kubera.notification'].sudo().create({
                        'title': 'Project Updated',
                        'message': f'Project "{project.name}" has been updated.',
                        'user_id': request.env.uid,
                        'priority': '1',
                        'res_model': 'project.project',
                        'res_id': project.id,
                    })
                except Exception:
                    pass
                try:
                    if kwargs.get('meeting_body') and kwargs.get('meeting_to_mails'):
                        email_body = kwargs.get('meeting_body', '')
                        meeting_info_html = f"""
                                            <div style="margin-top: 30px; padding: 15px; border: 1px solid #e1e1e1; background-color: #f9f9f9; border-radius: 5px; font-family: sans-serif;">
                                                <h4 style="margin-top: 0; color: #714B67;">📅 Meeting Information</h4>
                                                <p><b>Date/Time:</b> {kwargs.get('meeting_date', 'TBD')}</p>
                                                <p><b>Agenda:</b> {kwargs.get('meeting_agenda', 'N/A')}</p>
                                                <p><b>Meeting Link:</b> <a href="{kwargs.get('meeting_link', '#')}" style="color: #008784; text-decoration: none;">{kwargs.get('meeting_link')}</a></p>
                                            </div>
                                        """
                        full_body = f"{email_body}{meeting_info_html}"

                        mail_values = {
                            'subject': kwargs.get('meeting_subject', f"Meeting Invitation: {project.name}"),
                            'body_html': full_body,
                            'email_to': kwargs.get('meeting_to_mails'),
                            'email_cc': kwargs.get('meeting_cc_mails'),
                            'email_add_signature': False,
                            'email_bcc': kwargs.get('meeting_bcc_mails'),
                            'attachment_ids': [(6, 0, project.meeting_attachments.ids)] if project.meeting_attachments else [],
                            'auto_delete': False,
                        }

                        mail = request.env['mail.mail'].sudo().create(mail_values)
                        mail.send()
                except Exception as e:
                    print(f"Error While Sending Mail: {e}")

            return return_Response(
                message="Project Updated Successfully",
                status=200,
                data={'project_id': project.id}
            )

        except Exception as e:
            return return_Response(message="Update Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_project_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_project_list(self, **kwargs):
        try:
            domain = []
            search = kwargs.get('search')
            if search:
                domain += ['|', ('name', 'ilike', search), ('internal_project_name', 'ilike', search)]
            status_ids = kwargs.get('status')
            if status_ids:
                status_list = [int(x) for x in status_ids.split(',') if x.strip()]
                domain += [('stage_id', 'in', status_list)]

            client = kwargs.get('client')
            if client:
                domain += [('client_name', 'ilike', client)]
            # 5. Type & Flow Filters
            p_type = kwargs.get('type')
            if p_type:
                domain += [('project_type', '=', p_type)]

            y_p_type = kwargs.get('project_type')
            if y_p_type:
                domain += [('y_project_type', '=', y_p_type)]

            # 6. Pagination Logic
            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 10
            offset = (page - 1) * limit
            total_count = request.env['project.project'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0
            # 7. Fetch Records
            projects = request.env['project.project'].sudo().search(domain, limit=limit, offset=offset, order="create_date desc")

            # 8. Format Data to match the Screenshot Table
            project_data = []
            for p in projects:
                # Calculate Team Count (AIRE + SWE + Leads)
                team_ids = (p.project_lead.ids + p.project_aire.ids + p.project_swe.ids)
                unique_team_count = len(set(team_ids))
                project_data.append({
                    'id': safe_get_value(p, 'id', 'int'),
                    'project_name': safe_get_value(p, 'name', 'str'),
                    'project_id_code': safe_get_value(p, 'project_seq', 'str'),
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': 0, #getattr(p, 'progress_percentage', 0)
                    'tasks': safe_get_value(p, 'sample_task_number', 'int'),
                    'team_count': unique_team_count,
                    'blockers': 0,
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date')
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": total_count, "count": len(project_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_project_status', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_project_status(self, **kwargs):
        try:
            domain = []
            # page = int(kwargs.get('page')) if kwargs.get('page') else 1
            # limit = int(kwargs.get('limit')) if kwargs.get('limit') else 1
            # offset = (page - 1) * limit
            stage_list = []
            if kwargs.get('project_type') in ['technical', 'stem']:
                total_count = request.env['project.project.stage'].sudo().search_count(domain)
                if not kwargs.get('page'):
                    limit = total_count
                    offset = 0
                projects = request.env['project.project.stage'].sudo().search(domain, limit=limit, offset=offset, order="create_date desc")
                project_data = {}
                for p in projects:
                    if p.lable_name not in project_data.keys():
                        project_data[p.lable_name] = [{
                            'id': safe_get_value(p, 'id', 'int'),
                            'name': safe_get_value(p, 'name', 'str'),
                        }]
                    else:
                        project_data[p.lable_name].append({
                            'id': safe_get_value(p, 'id', 'int'),
                            'name': safe_get_value(p, 'name', 'str'),
                        })
                for stage in project_data.keys():
                    stage_list.append({
                        "phase": stage,
                        "items": project_data[stage]
                    })
            else:
                stage_list.append({
                    "phase": "non_stem",
                    "items": [{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_10').id,
                        "name": 'Production'
                    },{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_15').id,
                        "name": 'Paused'
                    },{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_13').id,
                        "name": 'Closed'
                    },{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_16').id,
                        "name": 'Cancel'
                    },{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_14').id,
                        "name": 'Draft'
                    },{
                        'id': request.env.ref('project_extension.project_project_stage_ethara_4').id,
                        "name": 'Not Started'
                    }]
                })

            return return_Response(
                message="Success",
                status=200,
                data={"record": stage_list, "total_record_count": len(stage_list), "count": len(stage_list)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_project_detail_view', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({"id": {"type": "str", "required": True}})
    def get_project_detail_view(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            project = request.env['project.project'].sudo().browse(int(jdata.get("id")))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)
            record = {
                'id': safe_get_value(project, 'id', 'int'),
                'name': safe_get_value(project, 'name', 'str'),
                'project_seq': safe_get_value(project, 'project_seq', 'str'),
                'internal_project_name': safe_get_value(project, 'internal_project_name', 'str'),
                "client_name": safe_get_value(project, 'client_name', 'str'),
                "project_category": safe_get_value(project, 'project_category', 'str'),
                "project_type": safe_get_value(project, 'project_type', 'str'),
                "sample_task_number": safe_get_value(project, 'sample_task_number', 'int'),
                "kick_off_to_mails": safe_get_value(project, 'kick_off_to_mails', 'str'),
                "kick_off_subject": safe_get_value(project, 'kick_off_subject', 'str'),
                "kick_off_body": safe_get_value(project, 'kick_off_body', 'str'),
                "ai_generated_description": safe_get_value(project, 'ai_generated_description', 'str'),
                "internal_client_name": safe_get_value(project, 'internal_client_name', 'str'),
                "y_project_type": safe_get_value(project, 'y_project_type', 'str'),
                "date_start": safe_get_value(project, 'date_start', 'str'),
                "date_end": safe_get_value(project, 'date', 'str'),
                "description": safe_get_value(project, 'description', 'str'),
                "whatsapp_group_name": safe_get_value(project, 'whatsapp_group_name', 'str'),
                "slack_channel_name": safe_get_value(project, 'slack_channel_name', 'str'),
                "whatsapp_group_members": [{'name': i.name, 'email': i.email, "mobile": i.phone_number, "country_code": i.country_code} for i in project.whatsapp_group_members],
                'stage_id': safe_get_value(project, 'stage_id.id', 'int'),
                "stage_name": safe_get_value(project, 'stage_id.name', 'str'),
                "project_lead": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.project_lead],
                "project_aire": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.project_aire],
                "project_swe": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.project_swe],
                "project_qc_reviewer": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.project_qc_reviewer],
                "project_tasker": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.project_tasker],
                "slack_members": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.slack_members],
                "google_drive_id": project.google_drive_id.get_drive_data() if project.google_drive_id else {},
                "is_pl_stage_completed": safe_get_value(project, 'is_pl_stage_completed', 'bool'),
                "is_aire_stage_completed": safe_get_value(project, 'is_aire_stage_completed', 'bool'),
                "is_swe_stage_completed": safe_get_value(project, 'is_swe_stage_completed', 'bool'),
                "project_guide_lines": [i.get_drive_data() for i in project.project_guide_lines],
                "project_experiment_design": [i.get_drive_data() for i in project.project_experiment_design],
                "project_research_document": [i.get_drive_data() for i in project.project_research_document],
                "project_infrastructure_requirement": [i.get_drive_data() for i in project.project_infrastructure_requirement],
                "skill_tags": [{"id": i.id, "name": i.name} for i in project.skill_tags],
                "ai_recommendation_tags": [{"id": i.id, "name": i.name} for i in project.ai_recommendation_tags],
                "research_notes": safe_get_value(project, 'research_notes', 'str'),
                "lock_ttl": safe_get_value(project, 'lock_ttl', 'int'),
                "daily_quota_per_tasker": safe_get_value(project, 'daily_quota_per_tasker', 'int'),
                "rating_configuration": safe_get_value(project, 'rating_configuration', 'str'),
                "meeting_date": safe_get_value(project, 'meeting_date', 'str'),
                "meeting_link": safe_get_value(project, 'meeting_link', 'str'),
                "meeting_agenda": safe_get_value(project, 'meeting_agenda', 'str'),
                "meeting_to_mails": safe_get_value(project, 'meeting_to_mails', 'str'),
                "meeting_cc_mails": safe_get_value(project, 'meeting_cc_mails', 'str'),
                "meeting_bcc_mails": safe_get_value(project, 'meeting_bcc_mails', 'str'),
                "meeting_subject": safe_get_value(project, 'meeting_subject', 'str'),
                "meeting_body": safe_get_value(project, 'meeting_body', 'str'),
                "is_response_required": project.is_response_required or False,
                "no_of_responses": project.no_of_responses or 0,
                "response_configs": [{
                    'id': cfg.id,
                    'label': cfg.label or '',
                    'sequence': cfg.sequence,
                } for cfg in project.response_config_ids.sorted('sequence')],
                "is_rubrics_required": project.is_rubrics_required or False,
                "is_justification_required": project.is_justification_required or False,
                "rubric_categories": [{
                    'id': cat.id,
                    'name': cat.name or '',
                    'description': cat.description or '',
                    'sequence': cat.sequence,
                    'options': [{
                        'id': opt.id,
                        'name': opt.name or '',
                        'value': opt.value,
                        'sequence': opt.sequence,
                    } for opt in cat.option_ids],
                    'dimensions': [{
                        'id': dim.id,
                        'name': dim.name or '',
                        'description': dim.description or '',
                        'is_required': dim.is_required,
                        'sequence': dim.sequence,
                        'options': [{
                            'id': o.id,
                            'name': o.name or '',
                            'value': o.value,
                        } for o in dim.option_ids],
                    } for dim in cat.dimension_ids],
                } for cat in project.rubric_category_ids],
            }
            return return_Response(
                message="Success",
                status=200,
                data={"record": record})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])


    @validate_token
    @http.route('/api/v1/get_project_customer_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_project_customer_list(self, **kwargs):
        try:
            project_customer = request.env['project.project'].sudo().search([]).mapped('client_name')
            return return_Response(
                message="Success",
                status=200,
                data={"record": list(set(filter(None, project_customer))) if project_customer else []})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_project_skill_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_project_skill_list(self, **kwargs):
        try:
            project_skills = request.env['project.skills'].sudo().search([])
            temp = []
            for pk in project_skills:
                temp.append({
                    'id': pk.id,
                    'name': pk.name
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": temp, "count": len(temp)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/update_project_pl_portal_skill_teams', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    def update_project_pl_portal_skill_teams(self, **kwargs):
        try:
            project = request.env['project.project'].sudo().browse(int(kwargs.get('project_id')))
            if not project.exists():
                return return_Response(message=f"Project {kwargs.get('project_id')} Not Found", status=404)

            rfp_stage = request.env.ref('project_extension.project_project_stage_ethara_4', raise_if_not_found=False)
            if rfp_stage and project.stage_id.id != rfp_stage.id:
                return return_Response(message="Project is not in the required RFP stage.", status=400)
            # Skills And Team Assign
            vals = {
                'project_qc_reviewer': [(6, 0, kwargs.get('project_qc_reviewer'))],
                'project_tasker': [(6, 0, kwargs.get('project_tasker'))],
                'skill_tags': [(6, 0, kwargs.get('skill_tags'))]
            }
            # Document Upload
            if request.httprequest.files.getlist('files'):
                attachment_ids = []
                files = request.httprequest.files.getlist('files')

                for file in files:
                    file_content = file.read()
                    if not file_content:
                        continue
                    drive_records = self.env['google.drive.file'].sudo().search([('type', '=', 'folder'), ('parent_id', '=', project.google_drive_id.id)])

                    for drive_id in drive_records:
                        drive_record = self.env['google.drive.wizard'].create({
                            'name': file.filename,
                            'upload_type': 'file',
                            'file_content': base64.b64encode(file_content),
                            'parent_folder_id': drive_id.id if drive_id else None,
                        })._upload_file()
                        attachment_ids.append(drive_record.id)
                if attachment_ids:
                    vals['project_guide_lines'] = [(6, 0, attachment_ids)]

            # Schedule Meeting
            if kwargs.get('meeting_date'):
                vals['meeting_date'] = kwargs.get('meeting_date')
            if kwargs.get('meeting_link'):
                vals['meeting_link'] = kwargs.get('meeting_link')
            if kwargs.get('meeting_agenda'):
                vals['meeting_agenda'] = kwargs.get('meeting_agenda')
            if kwargs.get('meeting_to_mails'):
                vals['meeting_to_mails'] = kwargs.get('meeting_to_mails')
            if kwargs.get('meeting_cc_mails'):
                vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
            if kwargs.get('meeting_bcc_mails'):
                vals['meeting_bcc_mails'] = kwargs.get('meeting_bcc_mails')
            if kwargs.get('meeting_subject'):
                vals['meeting_subject'] = kwargs.get('meeting_subject')
            if kwargs.get('meeting_body'):
                vals['meeting_body'] = kwargs.get('meeting_body')
            if kwargs.get('meeting_cc_mails'):
                vals['meeting_cc_mails'] = kwargs.get('meeting_cc_mails')
            meeting_attachments = request.httprequest.files.getlist('meeting_attachments')
            if meeting_attachments:
                attachment_ids = []
                for file in meeting_attachments:
                    file_content = file.read()
                    if not file_content: continue

                    attachment = request.env['ir.attachment'].sudo().create({
                        'name': file.filename,
                        'datas': base64.b64encode(file_content),
                        'res_model': 'project.project',
                        'res_id': project.id,
                        'type': 'binary',
                        'mimetype': file.content_type
                    })
                    attachment_ids.append(attachment.id)
                if attachment_ids:
                    vals['meeting_attachments'] = [(6, 0, attachment_ids)]

            project.sudo().write(vals)
            try:
                email_body = kwargs.get('meeting_body', '')
                meeting_info_html = f"""
                    <div style="margin-top: 30px; padding: 15px; border: 1px solid #e1e1e1; background-color: #f9f9f9; border-radius: 5px; font-family: sans-serif;">
                        <h4 style="margin-top: 0; color: #714B67;">📅 Meeting Information</h4>
                        <p><b>Date/Time:</b> {kwargs.get('meeting_date', 'TBD')}</p>
                        <p><b>Agenda:</b> {kwargs.get('meeting_agenda', 'N/A')}</p>
                        <p><b>Meeting Link:</b> <a href="{kwargs.get('meeting_link', '#')}" style="color: #008784; text-decoration: none;">{kwargs.get('meeting_link')}</a></p>
                    </div>
                """
                full_body = f"{email_body}{meeting_info_html}"

                mail_values = {
                    'subject': kwargs.get('meeting_subject', f"Meeting Invitation: {project.name}"),
                    'body_html': full_body,
                    'email_to': kwargs.get('meeting_to_mails'),
                    'email_cc': kwargs.get('meeting_cc_mails'),
                    'email_add_signature': False,
                    'email_bcc': kwargs.get('meeting_bcc_mails'),
                    'attachment_ids': [(6, 0, project.meeting_attachments.ids)] if project.meeting_attachments else [],
                    'auto_delete': False,
                }

                mail = request.env['mail.mail'].sudo().create(mail_values)
                mail.send()
            except Exception as e:
                print(f"Error While Sending Mail: {e}")

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'PL Portal Updated',
                    'message': f'Project "{project.name}" PL portal skills & teams updated.',
                    'user_id': request.env.uid,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(message="Project PL Portal Updated Successfully.", status=200)
        except Exception as e:
            return return_Response(message="Operation Failed", status=500, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/update_project_details_aire_portal_info', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    def update_project_details_aire_portal_info(self, **kwargs):
        try:
            if not kwargs.get('project_id'):
                return return_Response(message=f"Project Id is missing in the request body.", status=400)
            project_id = kwargs.get('project_id')

            project = request.env['project.project'].sudo().browse(int(project_id))
            if not project.exists():
                return return_Response(message=f"Project {project_id} Not Found", status=404)

            rfp_stage = request.env.ref('project_extension.project_project_stage_ethara_4', raise_if_not_found=False)
            if rfp_stage and project.stage_id.id != rfp_stage.id:
                return return_Response(message="Project is not in the required RFP stage.", status=400)

            if not project.is_pl_stage_completed:
                return return_Response(message="The Project PL Portal details are pending.", status=400)

            vals = {}
            skill_tags = []
            if kwargs.get('skill_tags'):
                for skill in kwargs.get('skill_tags'):
                    p_skill = request.env['project.skills'].sudo().search([('name', '=', skill)], limit=1)
                    if not p_skill:
                        p_skill = request.env['project.skills'].sudo().create({'name': skill})
                    skill_tags.append(p_skill.id)
            if skill_tags:
                vals['skill_tags'] = [(6, 0, skill_tags)]

            ai_recommendation_tags = []
            if kwargs.get('ai_recommendation_tags'):
                for recommendation in kwargs.get('ai_recommendation_tags'):
                    p_recommendation = request.env['project.ai.recommendation'].sudo().search([('name', '=', recommendation)], limit=1)
                    if not p_recommendation:
                        p_recommendation = request.env['project.ai.recommendation'].sudo().create({'name': recommendation})
                    ai_recommendation_tags.append(p_recommendation.id)
            if ai_recommendation_tags:
                vals['ai_recommendation_tags'] = [(6, 0, ai_recommendation_tags)]

            if kwargs.get('research_notes'):
                vals['research_notes'] = kwargs.get('research_notes')

            if request.httprequest.files.getlist('experiment_files'):
                attachment_ids = []
                files = request.httprequest.files.getlist('experiment_files')

                for file in files:
                    file_content = file.read()
                    if not file_content:
                        continue
                    drive_records = self.env['google.drive.file'].sudo().search([('type', '=', 'folder'), ('parent_id', '=', project.google_drive_id.id)])

                    for drive_id in drive_records:
                        drive_record = self.env['google.drive.wizard'].create({
                            'name': file.filename,
                            'upload_type': 'file',
                            'file_content': base64.b64encode(file_content),
                            'parent_folder_id': drive_id.id if drive_id else None,
                        })._upload_file()
                        attachment_ids.append(drive_record.id)
                if attachment_ids:
                    vals['project_experiment_design'] = [(6, 0, attachment_ids)]

            if request.httprequest.files.getlist('research_document'):
                attachment_ids = []
                files = request.httprequest.files.getlist('research_document')

                for file in files:
                    file_content = file.read()
                    if not file_content:
                        continue
                    drive_records = self.env['google.drive.file'].sudo().search([('type', '=', 'folder'), ('parent_id', '=', project.google_drive_id.id)])

                    for drive_id in drive_records:
                        drive_record = self.env['google.drive.wizard'].create({
                            'name': file.filename,
                            'upload_type': 'file',
                            'file_content': base64.b64encode(file_content),
                            'parent_folder_id': drive_id.id if drive_id else None,
                        })._upload_file()
                        attachment_ids.append(drive_record.id)
                if attachment_ids:
                    vals['project_research_document'] = [(6, 0, attachment_ids)]
            if vals:
                vals['is_aire_stage_completed'] = True
                project.sudo().write(vals)

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'AIRE Portal Updated',
                    'message': f'Project "{project.name}" AIRE portal details updated.',
                    'user_id': request.env.uid,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(message="Project AIRE Portal Updated Successfully.", status=200)

        except Exception as e:
            return return_Response(message="Operation Failed", status=500, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/update_project_details_swe_portal_info', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    def update_project_details_swe_portal_info(self, **kwargs):
        try:
            if not kwargs.get('project_id'):
                return return_Response(message=f"Project Id is missing in the request body.", status=400)
            project_id = kwargs.get('project_id')

            project = request.env['project.project'].sudo().browse(int(project_id))
            if not project.exists():
                return return_Response(message=f"Project {project_id} Not Found", status=404)

            rfp_stage = request.env.ref('project_extension.project_project_stage_ethara_4', raise_if_not_found=False)
            if rfp_stage and project.stage_id.id != rfp_stage.id:
                return return_Response(message="Project is not in the required RFP stage.", status=400)

            if not project.is_aire_stage_completed:
                return return_Response(message="The Project AIRE Portal Details are pending.", status=400)

            vals = {}
            if kwargs.get('rating_configuration'):
                vals['rating_configuration'] = kwargs['rating_configuration']

            if kwargs.get('task_template_type'):
                vals['task_template_type'] = int(kwargs['task_template_type'])

            if kwargs.get('lock_ttl'):
                vals['lock_ttl'] = int(kwargs['lock_ttl'])

            if kwargs.get('daily_quota_per_tasker'):
                vals['daily_quota_per_tasker'] = int(kwargs['daily_quota_per_tasker'])

            if request.httprequest.files.getlist('requirement_files'):
                attachment_ids = []
                files = request.httprequest.files.getlist('requirement_files')

                for file in files:
                    file_content = file.read()
                    if not file_content:
                        continue
                    drive_records = self.env['google.drive.file'].sudo().search([('type', '=', 'folder'), ('parent_id', '=', project.google_drive_id.id)])

                    for drive_id in drive_records:
                        drive_record = self.env['google.drive.wizard'].create({
                            'name': file.filename,
                            'upload_type': 'file',
                            'file_content': base64.b64encode(file_content),
                            'parent_folder_id': drive_id.id if drive_id else None,
                        })._upload_file()
                        attachment_ids.append(drive_record.id)
                if attachment_ids:
                    vals['project_infrastructure_requirement'] = [(6, 0, attachment_ids)]
            if vals:
                vals['is_swe_stage_completed'] = True
                vals['stage_id'] = request.env.ref('project_extension.project_project_stage_ethara_6', raise_if_not_found=False).id
                project.sudo().write(vals)

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'SWE Portal Updated',
                    'message': f'Project "{project.name}" SWE portal details updated.',
                    'user_id': request.env.uid,
                    'priority': '1',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(message="Project SWE Portal Updated Successfully.", status=200)

        except Exception as e:
            return return_Response(message="Operation Failed", status=500, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_task_template_type', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_task_template_type(self, **kwargs):
        try:
            domain = []
            jdata = kwargs.get('jdata')
            page = int(jdata.get('page')) if jdata.get('page') else 1
            limit = int(jdata.get('limit')) if jdata.get('limit') else 1
            offset = (page - 1) * limit
            total_count = request.env['task.template.type'].sudo().search_count(domain)
            if not kwargs.get('page'):
                limit = total_count
                offset = 0
            template_type = request.env['task.template.type'].sudo().search(domain, limit=limit, offset=offset, order="create_date desc")
            template_type_data = []
            for task in template_type:
                template_type_data.append({
                    'id': safe_get_value(task, 'id', 'int'),
                    'name': safe_get_value(task, 'name', 'str'),
                    'project_key': safe_get_value(task, 'project_key', 'str'),
                    'model_name': safe_get_value(task, 'model_name', 'str'),
                    'mapping_field_name': safe_get_value(task, 'mapping_field_name', 'str')
                })
            return return_Response(
                message="Success",
                status=200,
                data={"record": template_type_data, "total_record_count": len(total_count), "count": len(template_type_data)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/action_start_project', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({
        "project_id": {"type": "integer", "required": True}
    })
    def action_start_project(self, **kwargs):
        try:
            project = request.env['project.project'].sudo().browse(int(kwargs['project_id']))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)
            project.sudo().write({
                'stage_id': request.env.ref('project_extension.project_project_stage_ethara_10').id,
                'non_stemp_project_status': 'production'
            })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Start',
                    'message': f'Project "{project.name}" has been Started.',
                    'user_id': request.env.uid,
                    'priority': '2',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Success",
                status=200)

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/action_pause_project', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({
        "project_id": {"type": "integer", "required": True}
    })
    def action_pause_project(self, **kwargs):
        try:
            project = request.env['project.project'].sudo().browse(int(kwargs['project_id']))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)
            project.sudo().write({
                'stage_id': request.env.ref('project_extension.project_project_stage_ethara_15').id,
                'non_stemp_project_status': 'paused'
            })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Paused',
                    'message': f'Project "{project.name}" has been paused.',
                    'user_id': request.env.uid,
                    'priority': '2',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Success",
                status=200)

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/action_close_project', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({
        "project_id": {"type": "integer", "required": True}
    })
    def action_close_project(self, **kwargs):
        try:
            project = request.env['project.project'].sudo().browse(int(kwargs['project_id']))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)
            project.sudo().write({
                'stage_id': request.env.ref('project_extension.project_project_stage_ethara_13').id,
                'non_stemp_project_status': 'closed'
            })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Closed',
                    'message': f'Project "{project.name}" has been closed.',
                    'user_id': request.env.uid,
                    'priority': '2',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Success",
                status=200)

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/action_cancel_project', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({
        "project_id": {"type": "integer", "required": True}
    })
    def action_cancel_project(self, **kwargs):
        try:
            project = request.env['project.project'].sudo().browse(int(kwargs['project_id']))
            if not project.exists():
                return return_Response(message="Project Not Found", status=404)
            project.sudo().write({
                'stage_id': request.env.ref('project_extension.project_project_stage_ethara_16').id,
                'non_stemp_project_status': 'cancel'
            })

            try:
                request.env['kubera.notification'].sudo().create({
                    'title': 'Project Cancelled',
                    'message': f'Project "{project.name}" has been cancelled.',
                    'user_id': request.env.uid,
                    'priority': '2',
                    'res_model': 'project.project',
                    'res_id': project.id,
                    'project_id': project.id,
                })
            except Exception:
                pass

            return return_Response(
                message="Success",
                status=200)

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/get_employee_designation', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def get_employee_designation(self, **kwargs):
        temp = []
        try:
            designations = request.env['hr.employee.designation'].sudo().search([])
            for i in designations:
                temp.append({
                    'id': i.id,
                    'name': i.name
                })
            return return_Response(
                message="Success",
                status=200, data={"record": temp, "total_record_count": len(temp)})

        except Exception as e:
            return return_Response(message="Fetch Failed", status=400, errors=[str(e)])
