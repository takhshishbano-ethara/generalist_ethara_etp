from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value

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
                    try:
                        uid = request.session.authenticate(request.env, credential)
                    except:
                        uid = request.session.authenticate(request.session.db, credential)
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

            uid = token_rec.user_id.id
            token_rec.unlink()

            access_token, refresh = request.env['api.access_token'].sudo().find_one_or_create_token(user_id=uid, create=True)

            res = {
                'access_token': access_token or "",
                'refresh_token': refresh or ""
            }
            return return_Response(message="Success", status=200, data=res)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])


    @http.route('/api/v1/auth_token_unlink', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def auth_token_unlink(self, **params):
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
