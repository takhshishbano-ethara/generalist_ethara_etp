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
            domain = []

            if jdata.get('target_user_id'):
                domain = [('user_id', '=', int(jdata.get('target_user_id')))]

            if jdata.get('date'):
                filter_date = datetime.strptime(jdata['date'], '%Y-%m-%d')
                domain.append(('create_date', '>=', filter_date))
                domain.append(('create_date', '<', filter_date + timedelta(days=1)))

            if jdata.get('start_date') and jdata.get('end_date'):
                start = datetime.strptime(jdata['start_date'], '%Y-%m-%d')
                end = datetime.strptime(jdata['end_date'], '%Y-%m-%d') + timedelta(days=1)
                domain.append(('create_date', '>=', start))
                domain.append(('create_date', '<', end))

            if jdata.get('id'):
                domain.append(('id', '=', int(jdata.get('id'))))
            if jdata.get('priority'):
                domain.append(('priority', '=', jdata.get('priority')))
            if jdata.get('project_id'):
                domain.append(('project_id', '=', int(jdata.get('project_id'))))
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
                    'write_date': date,
                    'redirect_url': safe_get_value(rec,'redirect_url', 'str'),
                    'project_id': rec.project_id.id if rec.project_id else 0,
                    'project_name': rec.project_id.name if rec.project_id else "",
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

    @validate_token
    @http.route(['/api/v1/get_notification_user_list'], methods=['GET'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def get_notification_user_list(self, **params):
        try:
            temp = []
            for user in request.env['res.users'].sudo().search([]):
                temp.append({
                    'id': user.id,
                    'name': user.name
                })
            return return_Response({
                "status_code": 200,
                "message": "Success",
                "record": temp
            }, 200)

        except Exception as e:
            msg = {"message": f"Something went wrong: {str(e)}", "status_code": 400}
            return return_Response(msg, 400)

    @validate_token
    @http.route(['/api/v1/get_notification_grouped'], methods=['GET'], type='http', auth='public', csrf=False, cors='*')
    @validate_request({})
    def get_notification_grouped(self, **params):
        """
        Returns notifications grouped: today-{date}, tomorrow-{date}, older.
        Optionally filtered by project_id.
        """
        try:
            IST_OFFSET = timedelta(hours=5, minutes=30)
            try:
                user_id = request.env['res.users'].sudo().browse(self.env.uid)
            except Exception:
                user_id = request.env['res.users'].sudo().browse(request.env.uid)

            jdata = params.get('jdata')
            Notification = request.env['kubera.notification'].sudo()

            base_domain = [('user_id', '=', user_id.id)]
            if jdata.get('target_user_id'):
                base_domain = [('user_id', '=', int(jdata.get('target_user_id')))]
            if jdata.get('target_emp_id'):
                target_emp_id = request.env['hr.employee'].sudo().browse(int(jdata.get('target_emp_id')))
                if target_emp_id:
                    base_domain = [('user_id', '=', target_emp_id.user_id.id)]
            if jdata.get('project_id'):
                base_domain.append(('project_id', '=', int(jdata.get('project_id'))))
            if jdata.get('priority'):
                base_domain.append(('priority', '=', jdata.get('priority')))
            if jdata.get('status'):
                if jdata.get('status') == 'read':
                    base_domain.append(('is_read', '=', True))
                else:
                    base_domain.append(('is_read', '=', False))

            now_ist = datetime.utcnow() + IST_OFFSET
            today_ist = now_ist.date()
            tomorrow_ist = today_ist + timedelta(days=1)

            today_start_utc = datetime.combine(today_ist, datetime.min.time()) - IST_OFFSET
            today_end_utc = today_start_utc + timedelta(days=1)
            tomorrow_start_utc = today_end_utc
            tomorrow_end_utc = tomorrow_start_utc + timedelta(days=1)

            def format_date_key(prefix, dt):
                return f"{prefix}-{dt.strftime('%-d-%B-%Y')}"

            def format_records(records):
                result = []
                for rec in records:
                    date_str = str(rec.create_date + IST_OFFSET) if rec.create_date else ""
                    result.append({
                        'id': safe_get_value(rec, 'id', 'int'),
                        'title': safe_get_value(rec, 'title', 'str'),
                        'message': safe_get_value(rec, 'message', 'str'),
                        'status': safe_get_value(rec, 'status', 'str'),
                        'res_model': safe_get_value(rec, 'res_model', 'str'),
                        'priority': safe_get_value(rec, 'priority', 'str'),
                        'res_id': safe_get_value(rec, 'res_id', 'int'),
                        'is_read': safe_get_value(rec, 'is_read', 'bool'),
                        'create_date': date_str,
                        'write_date': date_str,
                        'redirect_url': safe_get_value(rec, 'redirect_url', 'str'),
                        'project_id': rec.project_id.id if rec.project_id else 0,
                        'project_name': rec.project_id.name if rec.project_id else "",
                    })
                return result

            today_recs = Notification.search(
                base_domain + [('create_date', '>=', today_start_utc), ('create_date', '<', today_end_utc)],
                order='id desc',
            )
            tomorrow_recs = Notification.search(
                base_domain + [('create_date', '>=', tomorrow_start_utc), ('create_date', '<', tomorrow_end_utc)],
                order='id desc',
            )
            older_recs = Notification.search(
                base_domain + [('create_date', '<', today_start_utc)],
                order='id desc',
            )

            today_key = format_date_key("Today", today_ist)
            tomorrow_key = format_date_key("Tomorrow", tomorrow_ist)

            data = {
                today_key: format_records(today_recs),
                tomorrow_key: format_records(tomorrow_recs),
                'older': format_records(older_recs),
            }

            return return_Response({
                "status_code": 200,
                "message": "Success",
                "record": data,
                "today_count": len(today_recs),
                "tomorrow_count": len(tomorrow_recs),
                "older_count": len(older_recs)
            }, 200)

        except Exception as e:
            msg = {"message": f"Something went wrong: {str(e)}", "status_code": 400}
            return return_Response(msg, 400)
