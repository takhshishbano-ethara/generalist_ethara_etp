import logging
import re

from odoo import api, models

_logger = logging.getLogger(__name__)

ETHARA_MODELS = (
    'ethara.project',
    'ethara.project.budget',
    'ethara.project.budget.topup',
    'ethara.project.phase',
    'ethara.project.phase.request',
)

CONFIG_KEY_DOMAIN = 'ethara_project.mail_message_id_domain'
DEFAULT_DOMAIN = 'ethara.ai'

MSGID_RE = re.compile(r'^(<[^@]+@)([^>]+)(>)$')


class MailMessage(models.Model):
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        try:
            self._ethara_rewrite_message_id_domain(messages)
        except Exception:
            _logger.exception(
                "[ETHARA-THREAD] Failed to rewrite message_id domain "
                "for messages ids=%s", messages.ids,
            )
        return messages

    @api.model
    def _ethara_rewrite_message_id_domain(self, messages):
        target_domain = (
            self.env['ir.config_parameter'].sudo().get_param(
                CONFIG_KEY_DOMAIN, DEFAULT_DOMAIN,
            )
        ).strip()
        if not target_domain:
            return
        for msg in messages:
            if msg.model not in ETHARA_MODELS:
                continue
            if not msg.message_id:
                continue
            m = MSGID_RE.match(msg.message_id)
            if not m:
                continue
            if m.group(2) == target_domain:
                continue
            new_msgid = f"{m.group(1)}{target_domain}{m.group(3)}"
            msg.write({'message_id': new_msgid})
