from odoo import http
from odoo.http import request
from .utility import validate_request, validate_token, return_Response, safe_get_value
class NotificationController(http.Controller):

    @validate_token
    @http.route('/api/v1/update_user_appearance_notification', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def update_user_appearance_notification(self, **params):
        try:
            jdata = params.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            user_dict = {
                'in_app_notification': True if jdata.get('in_app_notification') in [1, '1'] else user_id.partner_id.in_app_notification,
                'email_notification': True if jdata.get('email_notification') in [1, '1'] else user_id.partner_id.email_notification,
                'push_notification': True if jdata.get('push_notification') in [1, '1'] else user_id.partner_id.push_notification
            }
            if user_dict:
                user_id.partner_id.sudo().write(user_dict)
            employee_dict = {
                'in_app_notification': True if jdata.get('in_app_notification') in [1, '1'] else user_id.employee_id.in_app_notification,
                'email_notification': True if jdata.get('email_notification') in [1, '1'] else user_id.employee_id.email_notification,
                'push_notification': True if jdata.get('push_notification') in [1, '1'] else user_id.employee_id.push_notification
            }
            notification_line = []
            for notification in jdata.get('notification_line'):
                notification_line.append((0, 0, {
                    'name': int(notification.get('name')),
                    'in_app_notification': True if notification.get('in_app_notification') in [1, '1'] else False,
                    'email_notification': True if notification.get('email_notification') in [1, '1'] else False,
                    'push_notification': True if notification.get('push_notification') in [1, '1'] else False
                }))
            if notification_line:
                employee_dict['notification_line'] = [(5,)] + notification_line
            if employee_dict:
                user_id.employee_id.sudo().write(employee_dict)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="Updated Notification Successfully", status=200)

    @validate_token
    @http.route('/api/v1/update_user_appearance', methods=['POST'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def update_user_appearance(self, **params):
        try:
            jdata = params.get('jdata')
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            access_token = request.httprequest.headers.get('access_token')
            if not access_token:
                return return_Response(message="missing access token in request header", status=401)

            access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)
            if access_token_data:
                token_dict = {
                    'browser_name': jdata.get('browser_name') if jdata.get('browser_name') else access_token_data.browser_name,
                    'os_name': jdata.get('os_name') if jdata.get('os_name') else access_token_data.os_name,
                    'location': jdata.get('location') if jdata.get('location') else access_token_data.location,
                    'theme': jdata.get('theme') if jdata.get('theme') else access_token_data.theme,
                    'table_density': jdata.get('table_density') if jdata.get('table_density') else access_token_data.table_density,
                    'collapse_sidebar': True if jdata.get('collapse_sidebar') in [1, '1'] else access_token_data.collapse_sidebar
                }
                access_token_data.sudo().write(token_dict)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
        return return_Response(message="User Appearance Updated Successfully", status=200)

    @validate_token
    @http.route('/api/v1/get_notification_category_list', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({})
    def get_notification_category_list(self, **kwargs):
        temp = []
        try:
            jdata = kwargs.get('jdata')
            page = int(jdata.get('page')) if jdata.get('page') else 1
            limit = int(jdata.get('limit')) if jdata.get('limit') else 1
            offset = (page - 1) * limit
            domain = []
            employee_count = request.env['user.notification.category'].sudo().search_count(domain)
            if not jdata.get('page'):
                limit = employee_count
                offset = 0
            notification_category_list = request.env['user.notification.category'].sudo().search(domain, order='id DESC', limit=limit, offset=offset)
            for notification in notification_category_list:
                temp.append({
                    'id': safe_get_value(notification, 'id', 'int'),
                    'notification': safe_get_value(notification, 'name', 'str'),
                    'in_app_notification': safe_get_value(notification, 'in_app_notification', 'bool'),
                    'email_notification': safe_get_value(notification, 'email_notification', 'bool'),
                    'push_notification': safe_get_value(notification, 'push_notification', 'bool'),
                })

            return return_Response(message="Success", status=200, data={"record": temp, "total_record_count": employee_count, "count": len(temp)})
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])
