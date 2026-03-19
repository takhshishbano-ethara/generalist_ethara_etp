# -*- coding: utf-8 -*-
import json
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SlackChannelController(http.Controller):

    @http.route('/api/create_slack_channel', type='http', auth='public',
                methods=['POST'], csrf=False, cors='*')
    def create_slack_channel(self, **kwargs):
        """Create a Slack channel and invite members.

        Expected JSON body:
            {
                "channel_name": "my-channel",
                "admin_id": 2,
                "user_ids": [3, 4, 5]
            }

        Returns:
            JSON response with channel info or error.
        """
        try:
            # Parse request body
            try:
                body = json.loads(request.httprequest.data)
            except (json.JSONDecodeError, TypeError):
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': 'Invalid JSON body.',
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

            channel_name = body.get('channel_name', '').strip()
            admin_id = body.get('admin_id')
            user_ids = body.get('user_ids', [])

            # --- Validate required fields ---
            errors = []
            if not channel_name:
                errors.append('channel_name is required.')
            if not admin_id:
                errors.append('admin_id is required.')
            if not isinstance(user_ids, list):
                errors.append('user_ids must be a list of integers.')

            if errors:
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': errors,
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

            # Slack channel names: lowercase, no spaces, max 80 chars
            channel_name = channel_name.lower().replace(' ', '-')[:80]

            # --- Call the model method ---
            result = request.env['discuss.channel'].sudo().create_slack_channel(
                channel_name=channel_name,
                admin_id=int(admin_id),
                user_ids=[int(uid) for uid in user_ids],
            )

            if result.get('success'):
                return http.Response(
                    json.dumps({
                        'success': True,
                        'message': 'Slack channel created successfully.',
                        'data': {
                            'channel_id': result['channel_id'],
                            'channel_name': result['channel_name'],
                            'slack_channel_id': result['slack_channel_id'],
                            'members_invited': result['members_invited'],
                            'missing_users': result['missing_users'],
                            'invite_errors': result['invite_errors'],
                        },
                        'status': 200,
                    }),
                    content_type='application/json',
                    status=200,
                )
            else:
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': result.get('error', 'Unknown error'),
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

        except Exception as e:
            _logger.error("Error creating Slack channel: %s", str(e), exc_info=True)
            return http.Response(
                json.dumps({
                    'success': False,
                    'error': str(e),
                    'status': 500,
                }),
                content_type='application/json',
                status=500,
            )

    @http.route('/api/invite_slack_user', type='http', auth='public',
                methods=['POST'], csrf=False, cors='*')
    def invite_slack_user(self, **kwargs):
        """Invite an Odoo user to the Slack workspace.

        Sends a Slack workspace invitation to the user's email via
        the admin.users.invite API. Once they accept the invite and
        a Sync is run, their Slack account will be linked.

        Expected JSON body:
            {
                "user_id": 5
            }

        Returns:
            JSON with success/error status.
        """
        try:
            # Parse request body
            try:
                body = json.loads(request.httprequest.data)
            except (json.JSONDecodeError, TypeError):
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': 'Invalid JSON body.',
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

            user_id = body.get('user_id')
            if not user_id:
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': 'user_id is required.',
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

            # Look up Odoo user
            user = request.env['res.users'].sudo().browse(int(user_id))
            if not user.exists():
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': 'User ID %s not found.' % user_id,
                        'status': 404,
                    }),
                    content_type='application/json',
                    status=404,
                )

            # Call the model method
            result = user.invite_to_slack()

            if result.get('success'):
                return http.Response(
                    json.dumps({
                        'success': True,
                        'message': result.get('message', ''),
                        'data': {
                            'user_id': user.id,
                            'user_name': user.name,
                            'email': user.login,
                        },
                        'status': 200,
                    }),
                    content_type='application/json',
                    status=200,
                )
            else:
                return http.Response(
                    json.dumps({
                        'success': False,
                        'error': result.get('error', 'Unknown error'),
                        'status': 400,
                    }),
                    content_type='application/json',
                    status=400,
                )

        except Exception as e:
            _logger.error("Error inviting Slack user: %s", str(e),
                          exc_info=True)
            return http.Response(
                json.dumps({
                    'success': False,
                    'error': str(e),
                    'status': 500,
                }),
                content_type='application/json',
                status=500,
            )
