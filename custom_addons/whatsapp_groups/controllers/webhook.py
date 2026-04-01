# -*- coding: utf-8 -*-

import json
import logging

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WhatsAppGroupWebhook(http.Controller):
    """
    Handle webhook callbacks from the Meta WhatsApp Cloud API
    for group lifecycle events.

    Webhook URL: {odoo_base_url}/whatsapp_groups/webhook

    Supported events:
    - group_lifecycle_update (created, deleted)
    - group_participants_update (joined, left, removed)
    - group_settings_update (subject, description changes)
    """

    @http.route(
        '/whatsapp_groups/webhook',
        type='http',
        auth='none',
        methods=['GET'],
        csrf=False,
    )
    def webhook_verify(self, **kwargs):
        """
        Handle Meta webhook verification (GET request).
        Meta sends hub.mode, hub.verify_token, hub.challenge.
        """
        mode = kwargs.get('hub.mode')
        token = kwargs.get('hub.verify_token')
        challenge = kwargs.get('hub.challenge')

        if mode != 'subscribe' or not token or not challenge:
            return request.make_response('Bad Request', status=400)

        # Find the account matching this verify token
        account = request.env['whatsapp.group.account'].sudo().search(
            [('webhook_verify_token', '=', token)], limit=1,
        )
        if not account:
            _logger.warning("Webhook verify: no account found for token.")
            return request.make_response('Forbidden', status=403)

        _logger.info("Webhook verified for account: %s", account.name)
        return request.make_response(challenge, status=200)

    @http.route(
        '/whatsapp_groups/webhook',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def webhook_receive(self, **kwargs):
        """
        Receive and process webhook events from Meta.
        """
        try:
            body = request.httprequest.get_data(as_text=True)
            data = json.loads(body)
        except (ValueError, TypeError):
            _logger.error("Webhook: invalid JSON body")
            return request.make_response('Bad Request', status=400)

        _logger.debug("Webhook received: %s", json.dumps(data, indent=2))

        # Process each entry
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                field = change.get('field', '')
                value = change.get('value', {})
                self._process_webhook_event(field, value)

        return request.make_response('OK', status=200)

    def _process_webhook_event(self, field, value):
        """
        Route webhook events to appropriate handlers.

        :param field: Event field name (e.g. 'group_lifecycle_update')
        :param value: Event payload dict
        """
        handler_map = {
            'group_lifecycle_update': self._handle_lifecycle_update,
            'group_participants_update': self._handle_participants_update,
            'group_settings_update': self._handle_settings_update,
            'group_status_update': self._handle_status_update,
        }
        handler = handler_map.get(field)
        if handler:
            try:
                handler(value)
            except Exception:
                _logger.exception("Error processing webhook event: %s", field)
        else:
            _logger.debug("Unhandled webhook field: %s", field)

    def _handle_lifecycle_update(self, value):
        """Handle group created/deleted events."""
        group_id = value.get('group_id')
        event = value.get('event')
        if not group_id:
            return

        Group = request.env['whatsapp.group'].sudo()
        group = Group.search([('wa_group_id', '=', group_id)], limit=1)

        if event == 'deleted' and group:
            group.write({'state': 'deleted'})
            group.message_post(body="Group deleted (webhook notification).")
            _logger.info("Webhook: group %s marked as deleted.", group_id)

    def _handle_participants_update(self, value):
        """Handle participant joined/left/removed events."""
        group_id = value.get('group_id')
        if not group_id:
            return

        Group = request.env['whatsapp.group'].sudo()
        group = Group.search([('wa_group_id', '=', group_id)], limit=1)
        if not group:
            _logger.debug("Webhook: group %s not found locally.", group_id)
            return

        Participant = request.env['whatsapp.group.participant'].sudo()

        for participant in value.get('participants', []):
            wa_id = participant.get('wa_id')
            action = participant.get('action')  # 'joined', 'left', 'removed'

            if not wa_id:
                continue

            if action == 'joined':
                existing = Participant.search([
                    ('group_id', '=', group.id),
                    ('wa_id', '=', wa_id),
                ], limit=1)
                if existing:
                    existing.write({'active': True})
                else:
                    Participant.create({
                        'group_id': group.id,
                        'wa_id': wa_id,
                        'joined_at': fields.Datetime.now(),
                    })
                group.message_post(body="Participant %s joined." % wa_id)

            elif action in ('left', 'removed'):
                existing = Participant.search([
                    ('group_id', '=', group.id),
                    ('wa_id', '=', wa_id),
                    ('active', '=', True),
                ], limit=1)
                if existing:
                    existing.write({'active': False})
                group.message_post(
                    body="Participant %s %s the group." % (wa_id, action),
                )

    def _handle_settings_update(self, value):
        """Handle group subject/description changes."""
        group_id = value.get('group_id')
        if not group_id:
            return

        Group = request.env['whatsapp.group'].sudo()
        group = Group.search([('wa_group_id', '=', group_id)], limit=1)
        if not group:
            return

        vals = {}
        if 'subject' in value:
            vals['subject'] = value['subject']
        if 'description' in value:
            vals['description'] = value['description']
        if vals:
            group.write(vals)
            group.message_post(
                body="Group settings updated via webhook: %s" % ', '.join(vals.keys()),
            )

    def _handle_status_update(self, value):
        """Handle group status changes (placeholder for future use)."""
        _logger.info("Group status update: %s", value)
