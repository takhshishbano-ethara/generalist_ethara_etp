from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value

class ProjectController(http.Controller):

    @validate_token
    @http.route('/api/v1/get_employee_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_employee_list(self, **kwargs):
        temp = []
        try:
            jdata = kwargs.get('jdata')
            page = int(jdata.get('page', 1))
            limit = int(jdata.get('limit', 10))
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
            page = int(jdata.get('page', 1))
            limit = int(jdata.get('limit', 10))
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
