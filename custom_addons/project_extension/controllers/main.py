from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value
import base64
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
                    domain = [('designation_id', '=', request.env.ref('project_extension.designation_team_lead').id)]
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

            vals = {
                "name": kwargs.get("name"),
                "internal_project_name": kwargs.get("internal_project_name"),
                "client_name": kwargs.get("client_name"),
                "project_category": kwargs.get("project_category"),
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
                "internal_client_name": kwargs.get("internal_client_name"),
                "date_start": kwargs.get("date_start"),
                "date": kwargs.get("date_end"),
                'description': kwargs.get("description"),
                "whatsapp_group_name": kwargs.get('whatsapp_group_name'),
                'slack_channel_name': kwargs.get('slack_channel_name'),
                "slack_members": [(6, 0, parse_ids("slack_members"))],
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

            project = request.env['project.project'].sudo().create(vals)
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
            if kwargs.get('whatsapp_group_members'):
                wgm_list = []
                for rec in kwargs.get('whatsapp_group_members'):
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

            if project.stage_id.id == request.env.ref('project_extension.project_project_stage_ethara_14').id and not kwargs.get('stage_id'):
                vals['stage_id'] = request.env.ref('project_extension.project_project_stage_ethara_4').id
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
            if vals:
                project.sudo().write(vals)

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
                    'project_id_code': f"PRJ-{p.id:03}",
                    'client': safe_get_value(p, 'client_name', 'str'),
                    'status': safe_get_value(p, 'stage_id.name', 'str'),
                    'progress': 0, #getattr(p, 'progress_percentage', 0)
                    'tasks': safe_get_value(p, 'sample_task_number', 'int'),
                    'team_count': unique_team_count,
                    'blockers': 0,
                    'category': safe_get_value(p, 'project_category', 'str'),
                    'type': safe_get_value(p, 'project_type', 'str'),
                    'date_start': safe_get_value(p, 'date_start', 'date'),
                    'date_end': safe_get_value(p, 'date', 'date'),
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
            page = int(kwargs.get('page')) if kwargs.get('page') else 1
            limit = int(kwargs.get('limit')) if kwargs.get('limit') else 1
            offset = (page - 1) * limit
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
            return return_Response(
                message="Success",
                status=200,
                data={"record": project_data, "total_record_count": total_count, "count": len(project_data)})

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
                "slack_members": [{'id': safe_get_value(i, 'id', 'int'), 'name': safe_get_value(i, 'name', 'str')} for i in project.slack_members],
                "google_drive_id": project.google_drive_id.get_drive_data() if project.google_drive_id else {}
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
