from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value, generate_s3_link, is_valid_email, is_valid_mobile

class ApiAuthController(http.Controller):

    def get_the_menuitem_list(self, domain=[]):
        # 1. Fetch all records once
        menu_lines = request.env['api.role.line'].sudo().search(domain)
        user_line_ids = request.env.user.user_role.line_ids.ids if request.env.user.user_role else []

        menu_map = {}
        roots = []

        # 2. First Pass: Create the data objects
        for line in menu_lines:
            menu_map[line.id] = {
                'id': line.menu_name or "",
                'is_visible': line.id in user_line_ids,
                'order': line.sequence or 0,
                'read': line.can_read or False,
                'write': line.can_write or False,
                'create': line.can_create or False,
                'delete': line.can_delete or False,
                'parent_id': line.parent_id.id if line.parent_id else None,
                'child_list': []
            }

        # 3. Second Pass: The "Same Code" logic for any level
        for line_id, item in menu_map.items():
            parent_id = item['parent_id']

            if parent_id and parent_id in menu_map:
                menu_map[parent_id]['child_list'].append(item)
            else:
                # If no parent, it's a top-level root
                roots.append(item)

        # 4. Recursive Helper: Sorts and ensures Parents are visible if Children are
        def finalize_tree(items):
            items.sort(key=lambda x: x['order'])
            for item in items:
                if item['child_list']:
                    finalize_tree(item['child_list'])
                    # Logic: If any child is visible, the parent must be visible too
                    if any(child['is_visible'] for child in item['child_list']):
                        item['is_visible'] = True
            return items

        role_data = finalize_tree(roots)
        return role_data

    @http.route('/api/v1/auth_token', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({'login': {'type': 'str', 'required': True}, 'password': {'type': 'str', 'required': True}})
    def auth_token(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            login = jdata.get('login').lower().strip()
            password = jdata.get('password').strip()
            user = request.env['res.users'].sudo().search([('login', '=', login), ('active', 'in', [True, False])], limit=1, order='id desc')
            if user:
                if user.active:
                    credential = {'login': user.login, 'password': password, 'type': 'password'}
                    uid = request.session.authenticate(request.env, credential)
                else:
                    return return_Response(message="Your account has been deactivated. To reactivate it, please contact to the Administrator.", status=400)
            else:
                return return_Response(message="No user exists for the provided login credentials.", status=400)
        except Exception as e:
            return return_Response(message="Login failed. Please check your credentials.", status=400, errors=[str(e)])
        uid = uid['uid']
        if not uid:
            return return_Response(message="Login failed. Please check your credentials.", status=400)
        else:
            access_token, refresh  = request.env['api.access_token'].sudo().find_one_or_create_token(user_id=uid, create=True)
            if jdata.get('browser_name') or jdata.get('os_name') or jdata.get('location'):
                token_dict = {
                    'browser_name': jdata.get('browser_name'),
                    'os_name': jdata.get('os_name'),
                    'location': jdata.get('location')
                }
                if access_token:
                    token = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], limit=1)
                    if token:
                        token.sudo().write(token_dict)

            address = ""
            if request.env.user.partner_id.street:
                address += f"{request.env.user.partner_id.street}"
            if request.env.user.partner_id.city:
                address += f", {request.env.user.partner_id.city}"
            if request.env.user.partner_id.zip:
                address += f", {request.env.user.partner_id.zip}"
            if request.env.user.partner_id.state_id:
                address += f", {request.env.user.partner_id.state_id.name}"
            if request.env.user.partner_id.country_id:
                address += f", {request.env.user.partner_id.country_id.name}"
            role_data = self.get_the_menuitem_list(domain=[])
            res = {
                "data": {
                    'uid': uid,
                    'email': safe_get_value(request.env.user, 'login', 'str'),
                    'name': safe_get_value(request.env.user, 'name', 'str'),
                    'mobile': safe_get_value(request.env.user, 'phone', 'str'),
                    'access_token': access_token or "",
                    'refresh_token': refresh or "",
                    'address': address,
                    'user_role': safe_get_value(request.env.user, 'user_role.name', 'str'),
                    'profile_pic': "",
                    'permissions':role_data
                }
            }
            return return_Response(message="Success", status=200, data=res)

    @http.route('/api/v1/refresh_token', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({'refresh_token': {'type': 'str', 'required': True}})
    def refresh_token(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            token_rec = request.env['api.access_token'].sudo().search([('refresh_token', '=', jdata.get('refresh_token'))], limit=1)

            if not token_rec:
                return return_Response(message="Invalid Refresh Token", status=401)
            access_token, refresh = token_rec.update_access_token()
            res = {
                'access_token': access_token or "",
                'refresh_token': refresh or ""
            }
            return return_Response(message="Success", status=200, data=res)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @http.route('/api/v1/auth_token_unlink', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    def auth_token_unlink(self, **params):
        try:
            access_token = request.httprequest.headers.get('access_token')
            if not access_token:
                return return_Response(message="missing access token in request header", status=401)
            if access_token:
                access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)
                if access_token_data:
                    access_token_data.sudo().unlink()
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200)

    @http.route('/api/v1/sign_out_all_session', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def sign_out_all_session(self, **params):
        try:
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)
            access_token = request.env['api.access_token'].sudo().search([('user_id', '=', user_id.id)])
            if access_token:
                for token in access_token:
                    token.sudo().unlink()
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200)

    @http.route('/api/v1/get_menu_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_menu_list(self, **params):
        role_data = []
        try:
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)
            domain = []
            if params.get('id'):
                domain = [('menu_name', '=', params.get('id')), ('parent_id.menu_name', '=', params.get('id'))]
            role_data = self.get_the_menuitem_list(domain=domain)

        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Access Token Deleted Successfully", status=200, data={"permissions": role_data})

    @validate_token
    @http.route('/api/v1/update_profile_information', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def update_profile_information(self, **params):
        try:
            jdata = params.get('jdata')
            s3_connector_id = request.env['s3.connector'].sudo().search([], limit=1)
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            user_dict = {}
            partner_dict = {}

            if jdata.get('profile_pic'):
                partner_dict['profile_url'] = generate_s3_link(jdata.get('profile_pic'), uid=user_id.id) if f"{s3_connector_id.cdn_url}" not in jdata.get('profile_pic') else jdata.get('profile_pic')
            if jdata.get('name'):
                user_dict['name'] = jdata.get('name')
            if jdata.get('bio'):
                partner_dict['bio_data'] = jdata.get('bio')
            if jdata.get('location'):
                partner_dict['location'] = jdata.get('location')
            if jdata.get('email'):
                if not is_valid_email(jdata.get('email')):
                    return return_Response(message="Please enter a valid email address.", status=400, errors=[])
                else:
                    user_dict['email'] = jdata.get('email')
            if jdata.get('mobile'):
                if not is_valid_mobile(jdata.get('mobile')):
                    return return_Response(message="Please enter a valid mobile number.", status=400, errors=[])
                else:
                    user_dict['phone'] = jdata.get('mobile')

            # if jdata.get('new_password'):
            #     if not jdata.get('confirm_password') or not jdata.get('current_password'):
            #         return return_Response(message="Missing required parameter.", status=400, errors=[])
            #
            #     if jdata.get('new_password') != jdata.get('confirm_password'):
            #         return return_Response(message="The password and confirm password do not match.", status=400, errors=[])
            #
            #     credential = {'login': user_id.login, 'password': jdata.get('current_password'), 'type': 'password'}
            #     uid = request.session.authenticate(
            #         request.session.db,
            #         credential
            #     )
            #     if 'uid' not in uid:
            #         return return_Response(message="Incorrect Password.", status=400, errors=[])
            #     user_dict['password'] = jdata.get('new_password')

            if user_dict:
                user_id.sudo().write(user_dict)
            if partner_dict:
                user_id.partner_id.sudo().write(partner_dict)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Profile Updated Successfully", status=200)

    @validate_token
    @http.route('/api/v1/change_users_password', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({'old_password': {'type': 'str', 'required': True}, 'new_password': {'type': 'str', 'required': True}, 'confirm_password': {'type': 'str', 'required': True}})
    def change_users_password(self, **params):
        try:
            jdata = params.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            if jdata.get('new_password') != jdata.get('confirm_password'):
                return return_Response(message="The password and confirm password do not match.", status=400, errors=[])
            credential = {'login': user_id.login, 'password': jdata.get('old_password'), 'type': 'password'}
            uid = request.session.authenticate(request.env, credential)
            if 'uid' not in uid:
                return return_Response(message="Incorrect Password.", status=400, errors=[])
            user_id.sudo().password = jdata.get('new_password')
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Profile Updated Successfully", status=200)

    @validate_token
    @http.route('/api/v1/get_logged_user_details', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_logged_user_details(self, **kwargs):
        try:
            jdata = kwargs.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            projects = request.env['project.project'].sudo().search(['|', '|', '|', '|', ('project_lead', 'in', [user_id.employee_id.id]), ('project_aire', 'in', [user_id.employee_id.id]), ('project_swe', 'in', [user_id.employee_id.id]), ('project_qc_reviewer', 'in', [user_id.employee_id.id]), ('project_tasker', 'in', [user_id.employee_id.id])])

            data = {
                'id': safe_get_value(user_id, 'id', 'int'),
                'login': safe_get_value(user_id, 'login', 'str'),
                'name': safe_get_value(user_id, 'name', 'str'),
                'mobile': safe_get_value(user_id, 'phone', 'str'),
                'email': safe_get_value(user_id, 'email', 'str'),
                'employee_id': safe_get_value(user_id, 'employee_id.id', 'int'),
                'employee_name': safe_get_value(user_id, 'employee_id.name', 'str'),
                'department_id': safe_get_value(user_id, 'employee_id.department_id.id', 'int'),
                'department_name': safe_get_value(user_id, 'employee_id.department_id.name', 'str'),
                'education': f"{safe_get_value(user_id, 'employee_id.certificate', 'str')} {safe_get_value(user_id, 'employee_id.study_field', 'str')}",
                'experience_years': safe_get_value(user_id, 'employee_id.experience_years', 'float'),
                'profile_url': safe_get_value(user_id, 'partner_id.profile_url', 'str'),
                'bio_data': safe_get_value(user_id, 'partner_id.bio_data', 'str'),
                'location': safe_get_value(user_id, 'partner_id.street', 'str'),
                'in_app_notification': safe_get_value(user_id, 'employee_id.in_app_notification', 'bool'),
                'email_notification': safe_get_value(user_id, 'employee_id.email_notification', 'bool'),
                'push_notification': safe_get_value(user_id, 'employee_id.push_notification', 'bool'),
                'join_date': safe_get_value(user_id, 'employee_id.joining_date', 'str'),
                'project_count': len(projects),
                'team_size': 0,
                'blocked_resolved': 0,
                'avg_resolution': "",
                'skills': [{"id": skill.skill_id.id, "name": skill.skill_id.name} for skill in request.env['hr.employee.skill'].sudo().search([('employee_id', '=', user_id.employee_id.id)])],
                'project_list': [{'id': i.id, 'name': i.name, 'status': i.stage_id.name, "since": str(i.create_date)} for i in projects],
                'notification_line': []
            }
            for notification in user_id.employee_id.notification_line:
                data['notification_line'].append({
                    'name': safe_get_value(notification, 'name.name', 'str'),
                    'in_app_notification': safe_get_value(notification, 'in_app_notification', 'bool'),
                    'email_notification': safe_get_value(notification, 'email_notification', 'bool'),
                    'push_notification': safe_get_value(notification, 'push_notification', 'bool')
                })
            # Org Approval Rate
            data['approval_target'] = 95.0
            data['approval_graph'] = {"Jan": 65.5, "Feb": 67.0, "March": 90.2}
            access_token = request.httprequest.headers.get('access_token')
            if access_token:
                access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)
                if access_token_data:
                    data['browser_name'] = safe_get_value(access_token_data, 'browser_name', 'str')
                    data['os_name'] = safe_get_value(access_token_data, 'os_name', 'str')
                    data['location'] = safe_get_value(access_token_data, 'location', 'str')
                    data['theme'] = safe_get_value(access_token_data, 'theme', 'str')
                    data['table_density'] = safe_get_value(access_token_data, 'table_density', 'str')
                    data['collapse_sidebar'] = safe_get_value(access_token_data, 'collapse_sidebar', 'bool')
            return return_Response(message="Success", status=200, data={"record": data})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

