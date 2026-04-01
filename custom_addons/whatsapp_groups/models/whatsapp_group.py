# -*- coding: utf-8 -*-

import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsAppGroup(models.Model):
    _name = 'whatsapp.group'
    _description = 'WhatsApp Group'
    _inherit = ['mail.thread']
    _order = 'create_date desc'
    _rec_name = 'subject'

    account_id = fields.Many2one(
        'whatsapp.group.account',
        string="WhatsApp Account",
        required=True,
        ondelete='cascade',
    )
    subject = fields.Char(
        string="Group Subject",
        required=True,
        tracking=True,
        help="The name of the WhatsApp group (max 100 characters).",
    )
    description = fields.Text(
        string="Group Description",
        tracking=True,
        help="Description of the WhatsApp group (max 2048 characters).",
    )
    wa_group_id = fields.Char(
        string="WhatsApp Group ID",
        readonly=True,
        copy=False,
        help="Group ID returned by the Meta WhatsApp Cloud API.",
    )
    invite_link = fields.Char(
        string="Invite Link",
        readonly=True,
        copy=False,
    )
    join_approval_mode = fields.Selection(
        [
            ('none', 'Auto Approve'),
            ('admin_only', 'Admin Approval Required'),
        ],
        string="Join Approval",
        default='none',
        required=True,
        tracking=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('active', 'Active'),
            ('deleted', 'Deleted'),
        ],
        string="Status",
        default='draft',
        required=True,
        tracking=True,
        copy=False,
    )
    participant_ids = fields.One2many(
        'whatsapp.group.participant', 'group_id',
        string="Participants",
    )
    participant_count = fields.Integer(
        string="Participants",
        compute='_compute_participant_count',
    )

    @api.depends('participant_ids')
    def _compute_participant_count(self):
        for rec in self:
            rec.participant_count = len(rec.participant_ids)

    # -------------------------------------------------------------------------
    # API Actions
    # -------------------------------------------------------------------------

    def action_create_group(self):
        """Create the group on WhatsApp via Meta Cloud API."""
        self.ensure_one()
        if self.wa_group_id:
            raise UserError("This group has already been created on WhatsApp.")
        if not self.subject:
            raise UserError("Group subject is required.")

        payload = {
            'messaging_product': 'whatsapp',
            'subject': self.subject[:100],
        }
        if self.description:
            payload['description'] = self.description[:2048]
        if self.join_approval_mode:
            payload['join_approval_mode'] = self.join_approval_mode

        endpoint = "/%s/groups" % self.account_id.phone_number_id
        result = self.account_id._graph_api_request('POST', endpoint, payload=payload)

        group_id = result.get('group_id') or result.get('id')
        invite_link = result.get('invite_link', '')

        if not group_id:
            raise UserError(
                "Unexpected API response: no group_id returned.\nResponse: %s" % result
            )

        self.write({
            'wa_group_id': group_id,
            'invite_link': invite_link,
            'state': 'active',
        })
        self.message_post(
            body="Group created on WhatsApp. ID: %s" % group_id,
        )
        _logger.info("WhatsApp group created: %s (ID: %s)", self.subject, group_id)

    def action_refresh_invite_link(self):
        """Fetch the current invite link from WhatsApp."""
        self.ensure_one()
        self._check_active()
        endpoint = "/%s" % self.wa_group_id
        result = self.account_id._graph_api_request(
            'GET', endpoint, params={'fields': 'invite_link'},
        )
        new_link = result.get('invite_link', '')
        self.write({'invite_link': new_link})
        self.message_post(body="Invite link refreshed.")

    def action_reset_invite_link(self):
        """Reset (regenerate) the invite link on WhatsApp."""
        self.ensure_one()
        self._check_active()
        endpoint = "/%s" % self.wa_group_id
        payload = {'messaging_product': 'whatsapp'}
        result = self.account_id._graph_api_request('POST', endpoint, payload=payload)
        new_link = result.get('invite_link', '')
        self.write({'invite_link': new_link})
        self.message_post(body="Invite link has been reset.")

    def action_delete_group(self):
        """Delete the group on WhatsApp."""
        self.ensure_one()
        self._check_active()
        endpoint = "/%s" % self.wa_group_id
        payload = {'messaging_product': 'whatsapp'}
        self.account_id._graph_api_request('DELETE', endpoint, payload=payload)
        self.write({'state': 'deleted'})
        self.message_post(body="Group deleted from WhatsApp.")
        _logger.info("WhatsApp group deleted: %s (ID: %s)", self.subject, self.wa_group_id)

    def action_remove_participant(self):
        """Open wizard to select participants to remove (placeholder for future wizard)."""
        self.ensure_one()
        self._check_active()
        # Directly callable from participant records — see whatsapp_group_participant.py
        raise UserError(
            "To remove a participant, use the 'Remove' button on the participant line."
        )

    def action_copy_invite_link(self):
        """Return a client action to copy invite link to clipboard."""
        self.ensure_one()
        if not self.invite_link:
            raise UserError("No invite link available. Create the group first.")
        return {
            'type': 'ir.actions.act_url',
            'url': self.invite_link,
            'target': 'new',
        }

    def action_set_draft(self):
        """Reset to draft state (for re-creation)."""
        self.ensure_one()
        self.write({
            'state': 'draft',
            'wa_group_id': False,
            'invite_link': False,
        })

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _check_active(self):
        """Ensure the group is in active state with a valid WhatsApp ID."""
        self.ensure_one()
        if self.state != 'active' or not self.wa_group_id:
            raise UserError(
                "This action requires an active WhatsApp group. "
                "Please create the group first."
            )
