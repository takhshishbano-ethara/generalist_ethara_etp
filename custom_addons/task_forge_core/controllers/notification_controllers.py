from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, validate_request
)
import json


class TaskForgeNotificationController(http.Controller):

    @http.route('/api/v2/taskforge/notifications', methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def list_notifications(self, **kwargs):
        try:
            user = request.env.user
            Notification = request.env['kubera.notification'].sudo()
            notifications = Notification.search([
                ('user_id', '=', user.id),
            ], order='create_date desc', limit=50)

            data = [{
                'id': n.id,
                'title': n.title,
                'message': n.message,
                'is_read': n.is_read,
                'priority': n.priority,
                'created_at': n.create_date.isoformat() if n.create_date else '',
            } for n in notifications]

            return return_Response(message="Notifications", status=200, data={'data': data})
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/notifications/read', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    @validate_request({
        'notification_id': {'type': 'int', 'required': False},
    })
    def mark_read(self, jdata=None, **kwargs):
        try:
            user = request.env.user
            Notification = request.env['kubera.notification'].sudo()

            if jdata and jdata.get('notification_id'):
                notif = Notification.browse(jdata['notification_id'])
                if notif.exists() and notif.user_id.id == user.id:
                    notif.write({'is_read': True})
                return return_Response(message="Notification marked as read", status=200)
            else:
                # Mark all as read
                Notification.search([
                    ('user_id', '=', user.id),
                    ('is_read', '=', False),
                ]).write({'is_read': True})
                return return_Response(message="All notifications marked as read", status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)
