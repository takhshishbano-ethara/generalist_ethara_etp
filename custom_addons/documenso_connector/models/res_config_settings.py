# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .documenso_client import DocumensoClient


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    documenso_api_url = fields.Char(
        string='Documenso API URL',
        config_parameter='documenso_connector.api_url',
        default='https://app.documenso.com/api/v2',
        help='Base URL of the Documenso API (v2). Example: https://app.documenso.com/api/v2',
    )
    documenso_api_token = fields.Char(
        string='Documenso API Token',
        config_parameter='documenso_connector.api_token',
        help='API token generated in your Documenso account settings.',
    )
    documenso_web_url = fields.Char(
        string='Documenso Web URL',
        config_parameter='documenso_connector.web_url',
        default='https://app.documenso.com',
        help='Public URL used to build the "View in Documenso" links.',
    )
    documenso_signing_base_url = fields.Char(
        string='Documenso Signing URL',
        config_parameter='documenso_connector.signing_base_url',
        default='https://app.documenso.com/sign',
        help='Base URL used to build signing links for recipients.',
    )
    documenso_webhook_secret = fields.Char(
        string='Documenso Webhook Secret',
        config_parameter='documenso_connector.webhook_secret',
        help='Shared secret used to verify inbound Documenso webhook calls.',
    )
    documenso_rate_limit_ms = fields.Integer(
        string='Rate Limit (ms)',
        config_parameter='documenso_connector.rate_limit_ms',
        default=250,
    )
    documenso_page_size = fields.Integer(
        string='Sync Page Size',
        config_parameter='documenso_connector.page_size',
        default=25,
    )

    @api.model
    def get_client(self):
        params = self.env['ir.config_parameter'].sudo()
        try:
            rate_limit_ms = int(params.get_param('documenso_connector.rate_limit_ms') or 250)
        except (TypeError, ValueError):
            rate_limit_ms = 250
        return DocumensoClient(
            base_url=params.get_param('documenso_connector.api_url'),
            api_token=params.get_param('documenso_connector.api_token'),
            rate_limit_ms=rate_limit_ms,
            signing_base_url=params.get_param('documenso_connector.signing_base_url'),
            webhook_secret=params.get_param('documenso_connector.webhook_secret'),
        )

    def action_documenso_test_connection(self):
        self.ensure_one()
        client = self.get_client()
        client.ping()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Documenso',
                'message': 'Connection successful.',
                'type': 'success',
                'sticky': False,
            },
        }
