# -*- coding: utf-8 -*-

import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

import requests

_logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v23.0"


class WhatsAppGroupAccount(models.Model):
    _name = 'whatsapp.group.account'
    _description = 'WhatsApp Business Account for Groups'
    _rec_name = 'name'

    name = fields.Char(string="Account Name", required=True)
    phone_number_id = fields.Char(
        string="Phone Number ID",
        required=True,
        help="The Phone Number ID from your Meta WhatsApp Business Account.",
    )
    access_token = fields.Char(
        string="Access Token",
        required=True,
        help="Permanent access token with whatsapp_business_messaging permission.",
    )
    app_id = fields.Char(
        string="App ID",
        help="Meta App ID (optional, used for webhook verification).",
    )
    webhook_verify_token = fields.Char(
        string="Webhook Verify Token",
        help="Token used to verify incoming webhook requests from Meta.",
    )
    active = fields.Boolean(default=True)
    group_ids = fields.One2many(
        'whatsapp.group', 'account_id', string="Groups",
    )
    group_count = fields.Integer(
        string="Group Count", compute='_compute_group_count',
    )

    @api.depends('group_ids')
    def _compute_group_count(self):
        for rec in self:
            rec.group_count = len(rec.group_ids)

    def _get_headers(self):
        """Return authorization headers for the Meta Graph API."""
        self.ensure_one()
        if not self.access_token:
            raise UserError("Access token is not configured for account %s." % self.name)
        return {
            'Authorization': 'Bearer %s' % self.access_token,
            'Content-Type': 'application/json',
        }

    def _graph_api_request(self, method, endpoint, payload=None, params=None):
        """
        Make a request to the Meta Graph API.

        :param method: HTTP method (GET, POST, DELETE)
        :param endpoint: API endpoint path (e.g. '/{phone_number_id}/groups')
        :param payload: JSON body dict (for POST/DELETE)
        :param params: Query params dict (for GET)
        :returns: parsed JSON response
        :raises UserError: on API error
        """
        self.ensure_one()
        url = "%s%s" % (GRAPH_API_BASE, endpoint)
        headers = self._get_headers()

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=payload,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            error_data = {}
            try:
                error_data = exc.response.json()
            except (ValueError, AttributeError):
                pass
            error_msg = error_data.get('error', {}).get('message', str(exc))
            _logger.error(
                "WhatsApp Graph API error [%s %s]: %s",
                method, url, error_msg,
            )
            raise UserError("WhatsApp API Error: %s" % error_msg) from exc
        except requests.exceptions.RequestException as exc:
            _logger.error(
                "WhatsApp Graph API connection error [%s %s]: %s",
                method, url, exc,
            )
            raise UserError(
                "Could not connect to WhatsApp API. Please check your network and credentials."
            ) from exc

    def action_open_groups(self):
        """Open list of groups for this account."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'WhatsApp Groups',
            'res_model': 'whatsapp.group',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
            'context': {'default_account_id': self.id},
        }
