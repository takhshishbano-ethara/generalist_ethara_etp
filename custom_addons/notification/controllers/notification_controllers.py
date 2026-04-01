from odoo import http
from odoo.http import request
from .utility import return_Response, validate_request, safe_get_value, validate_token
from datetime import datetime, timedelta


class KuberhaNotificationRestAPI(http.Controller):

    @validate_token
    @http.route('/api/v1/create_notification', type='http', auth='public', methods=['POST'], csrf=False, cors='*')
    @validate_request({'title': {'type': 'str', 'required': True}, 'message': {'type': 'str', 'required': True}})
    def create_notification(self, **params):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            jdata = params.get('jdata')
            vals = {
                'title': jdata.get('title'),
                'message': jdata.get('message'),
                'status': jdata.get('status'),
                'res_model': jdata.get('res_model') or False,
                'res_id': int(jdata.get('res_id')) if jdata.get('res_id') else False,
                'user_id': user_id.id,
                'priority': jdata.get('priority') or '1',
                'is_read': False,
                'redirect_url': jdata.get('redirect_url') or ""
            }
            data = request.env['kubera.notification'].sudo().create(vals)
            return return_Response(message="Notification Created Successfully!", status=200)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route('/api/v1/update_notification', type='http', auth='public', methods=['POST'], csrf=False, cors='*')
    @validate_request({'id': {'type': 'int', 'required': True}})
    def update_notification(self, **params):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            jdata = params.get('jdata')
            notification = request.env['kubera.notification'].sudo().search([('id', '=', int(jdata.get('id')))], limit=1)
            if notification:
                vals = {
                    'title': jdata.get('title') if jdata.get('title') else notification.title,
                    'message': jdata.get('message') if jdata.get('message') else notification.message,
                    'status': jdata.get('status') if jdata.get('status') else notification.status,
                    'res_model': jdata.get('res_model') if jdata.get('res_model') else notification.res_model,
                    'res_id': int(jdata.get('res_id')) if jdata.get('res_id') else notification.res_id,
                    'priority': jdata.get('priority') if jdata.get('priority') else notification.priority,
                    'is_read': True,
                    'redirect_url': jdata.get('redirect_url') if jdata.get('redirect_url') else notification.redirect_url
                }
                data = notification.sudo().write(vals)
            return return_Response(message="Notification Updated Successfully!", status=200)
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[str(e)])

    @validate_token
    @http.route(['/api/v1/get_notification'], methods=['GET'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def get_notification(self, **params):
        try:
            temp = []
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)
            jdata = params.get('jdata')
            page = int(jdata.get('page', 1))
            limit = int(jdata.get('limit', 10))
            offset = (page - 1) * limit
            domain = [('user_id', '=', user_id.id)]
            if jdata.get('id'):
                domain.append(('id', '=', int(jdata.get('id'))))
            if jdata.get('priority'):
                domain.append(('priority', '=', jdata.get('priority')))
            if jdata.get('status'):
                if jdata.get('status') == 'read':
                    domain.append(('is_read', '=', True))
                else:
                    domain.append(('is_read', '=', False))
            record_count = request.env['kubera.notification'].sudo().search_count(domain)
            if not jdata.get('page'):
                limit = record_count
                offset = 0
            records = request.env['kubera.notification'].sudo().search(domain, order='id desc', limit=limit, offset=offset)
            read_message_count = request.env['kubera.notification'].sudo().search_count([('user_id', '=', user_id.id), ('is_read', '=', True)])
            unread_message_count = request.env['kubera.notification'].sudo().search_count([('user_id', '=', user_id.id), ('is_read', '=', False)])
            for rec in records:
                date = str(rec.create_date + timedelta(hours=5, minutes=30)) or ""
                vals = {
                    'id': safe_get_value(rec, 'id', 'int'),
                    'title':  safe_get_value(rec, 'title', 'str'),
                    'message':  safe_get_value(rec, 'message', 'str'),
                    'status':  safe_get_value(rec, 'status', 'str'),
                    'res_model':  safe_get_value(rec, 'res_model', 'str'),
                    'priority':  safe_get_value(rec, 'priority', 'str'),
                    'res_id':  safe_get_value(rec, 'res_id', 'int'),
                    'is_read': safe_get_value(rec, 'is_read', 'bool'),
                    'create_date': date,
                    # 'write_date': safe_get_value(rec,'write_date', 'datetime'),
                    'write_date': date,
                    'redirect_url': safe_get_value(rec,'redirect_url', 'str')
                }

                temp.append(vals)
            res = {
                "status_code": 200,
                "message": "Success",
                "record": temp,
                "record_count": record_count,
                "read_message_count": read_message_count,
                "unread_message_count": unread_message_count
            }
            return return_Response(res, 200)

        except Exception as e:
            msg = {"message": f"Something went wrong: {str(e)}", "status_code": 400}
            return return_Response(msg, 400)
