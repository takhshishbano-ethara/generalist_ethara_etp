# -*- coding: utf-8 -*-

import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsAppGroupParticipant(models.Model):
    _name = 'whatsapp.group.participant'
    _description = 'WhatsApp Group Participant'
    _rec_name = 'wa_id'

    group_id = fields.Many2one(
        'whatsapp.group',
        string="Group",
        required=True,
        ondelete='cascade',
    )
    wa_id = fields.Char(
        string="WhatsApp ID",
        required=True,
        help="Phone number in international format without + (e.g. 14155552671).",
    )
    display_name = fields.Char(
        string="Name",
        help="Contact name (optional, for display purposes).",
    )
    role = fields.Selection(
        [
            ('participant', 'Participant'),
            ('admin', 'Admin'),
        ],
        string="Role",
        default='participant',
    )
    joined_at = fields.Datetime(
        string="Joined At",
        readonly=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string="Contact",
        help="Link to an Odoo contact (optional).",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            'unique_participant_per_group',
            'UNIQUE(group_id, wa_id)',
            'A participant can only be added once per group.',
        ),
    ]

    def action_remove_from_group(self):
        """Remove this participant from the WhatsApp group via the API."""
        self.ensure_one()
        group = self.group_id
        group._check_active()

        endpoint = "/%s/participants" % group.wa_group_id
        payload = {
            'messaging_product': 'whatsapp',
            'participants': [{'wa_id': self.wa_id}],
        }
        group.account_id._graph_api_request('DELETE', endpoint, payload=payload)
        self.write({'active': False})
        group.message_post(
            body="Participant %s removed from group." % self.wa_id,
        )
        _logger.info(
            "Removed participant %s from WhatsApp group %s",
            self.wa_id, group.wa_group_id,
        )
