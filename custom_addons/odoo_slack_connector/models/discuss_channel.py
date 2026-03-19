# -*- coding: utf-8 -*-
#############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Cybrosys Techno Solutions(<https://www.cybrosys.com>)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
import logging
import requests
from requests import RequestException
from odoo import fields, models

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    """Inherited to add more field and functions"""
    _inherit = 'discuss.channel'

    is_slack = fields.Boolean(string="Slack", help="True for Slack connected")
    channel = fields.Char(string="Slack Reference",
                          help='Slack reference of channel')
    msg_date = fields.Datetime(string="Message Date", help="Date of message")
    slack_admin_id = fields.Many2one(
        'res.users', string="Slack Channel Admin",
        help="Odoo user who is the admin/creator of the Slack channel")

    def _get_slack_headers(self):
        """Return Slack API headers with bot token."""
        token = self.env.user.company_id.token
        if not token:
            raise ValueError("Slack Bot Token is not configured. Go to Settings → Company → Slack Configuration.")
        return {'Authorization': 'Bearer ' + token}

    def create_slack_channel(self, channel_name, admin_id, user_ids):
        """Create a Slack channel and invite members.

        Args:
            channel_name (str): Name for the Slack channel (lowercase, no spaces).
            admin_id (int): Odoo user ID for the channel admin.
            user_ids (list[int]): List of Odoo user IDs to invite as members.

        Returns:
            dict: Result with Odoo channel record and Slack channel info.
        """
        headers = self._get_slack_headers()

        # --- Validate admin ---
        admin_user = self.env['res.users'].sudo().browse(admin_id)
        if not admin_user.exists():
            return {'success': False, 'error': 'Admin user ID %s not found.' % admin_id}
        if not admin_user.slack_user_ref:
            return {'success': False, 'error': 'Admin user "%s" has no linked Slack account. Sync users first.' % admin_user.name}

        # --- Validate members and collect Slack IDs ---
        slack_user_ids = []
        missing_users = []
        users = self.env['res.users'].sudo().browse(user_ids)
        for user in users:
            if not user.exists():
                missing_users.append(str(user.id))
            elif not user.slack_user_ref:
                missing_users.append('%s (no Slack ID)' % user.name)
            else:
                slack_user_ids.append(user.slack_user_ref)

        # Include admin in invite list
        if admin_user.slack_user_ref not in slack_user_ids:
            slack_user_ids.append(admin_user.slack_user_ref)

        # --- Step 1: Create channel on Slack ---
        create_url = "https://slack.com/api/conversations.create"
        create_payload = {
            'name': channel_name,
            'is_private': False,
        }
        try:
            resp = requests.post(create_url, headers=headers, json=create_payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.ConnectionError:
            return {'success': False, 'error': 'Cannot reach Slack API. Check internet connection.'}
        except requests.exceptions.Timeout:
            return {'success': False, 'error': 'Slack API timed out. Try again.'}

        if not result.get('ok'):
            return {'success': False, 'error': 'Slack API error: %s' % result.get('error', 'Unknown')}

        slack_channel_id = result['channel']['id']
        slack_channel_name = result['channel']['name']

        # --- Step 2: Invite members ---
        invite_errors = []
        if slack_user_ids:
            invite_url = "https://slack.com/api/conversations.invite"
            invite_payload = {
                'channel': slack_channel_id,
                'users': ','.join(slack_user_ids),
            }
            try:
                invite_resp = requests.post(invite_url, headers=headers, json=invite_payload, timeout=15)
                invite_result = invite_resp.json()
                if not invite_result.get('ok'):
                    error = invite_result.get('error', 'Unknown')
                    # 'already_in_channel' is not a real error
                    if error != 'already_in_channel':
                        invite_errors.append(error)
                        _logger.warning("Slack invite error for channel %s: %s", slack_channel_id, error)
            except Exception as e:
                invite_errors.append(str(e))
                _logger.error("Failed to invite users to Slack channel %s: %s", slack_channel_id, e)

        # --- Step 3: Create Odoo discuss.channel record ---
        odoo_channel = self.sudo().create({
            'name': slack_channel_name,
            'channel': slack_channel_id,
            'is_slack': True,
            'slack_admin_id': admin_id,
            'channel_type': 'channel',
        })

        # Add members to the Odoo channel
        all_user_ids = list(set(user_ids + [admin_id]))
        odoo_users = self.env['res.users'].sudo().browse(all_user_ids)
        partner_ids = odoo_users.mapped('partner_id').ids
        existing_partner_ids = odoo_channel.channel_member_ids.mapped('partner_id.id')
        new_members = [
            (0, 0, {'partner_id': pid})
            for pid in partner_ids
            if pid not in existing_partner_ids
        ]
        if new_members:
            odoo_channel.sudo().write({'channel_member_ids': new_members})

        return {
            'success': True,
            'channel_id': odoo_channel.id,
            'channel_name': slack_channel_name,
            'slack_channel_id': slack_channel_id,
            'members_invited': len(slack_user_ids),
            'missing_users': missing_users,
            'invite_errors': invite_errors,
        }

    def action_sync_members(self):
        """Sync Slack channel members to Odoo discuss.channel members.

        Looks up each Slack member by slack_user_ref on res.users.
        Only adds members that are already linked Odoo users.
        Does NOT create contacts/partners for unknown Slack members.
        """
        headers = self._get_slack_headers()

        for channel in self:
            if not channel.is_slack or not channel.channel:
                continue
            try:
                url = ("https://slack.com/api/conversations.members"
                       "?channel=" + channel.channel)
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                data = response.json()

                if not data.get('ok'):
                    _logger.warning(
                        "Slack API error for channel %s: %s",
                        channel.channel, data.get('error', 'Unknown'))
                    continue

                slack_member_ids = data.get('members', [])

                # Find Odoo users that are linked to these Slack members
                linked_users = self.env['res.users'].sudo().search([
                    ('slack_user_ref', 'in', slack_member_ids)
                ])
                partner_ids = linked_users.mapped('partner_id').ids

                # Add only partners not already in this channel
                existing_partner_ids = channel.channel_member_ids.mapped(
                    'partner_id.id')
                new_members = [
                    (0, 0, {'partner_id': pid})
                    for pid in partner_ids
                    if pid not in existing_partner_ids
                ]
                if new_members:
                    channel.sudo().write(
                        {'channel_member_ids': new_members})

                skipped = len(slack_member_ids) - len(linked_users)
                if skipped > 0:
                    _logger.info(
                        "Channel %s: added %d members, skipped %d "
                        "(no linked Odoo user)",
                        channel.channel, len(new_members), skipped)

            except RequestException as e:
                _logger.error(
                    "Slack API request failed for channel %s: %s",
                    channel.channel, e)
            except (KeyError, ValueError) as e:
                _logger.error(
                    "Error parsing Slack response for channel %s: %s",
                    channel.channel, e)
            except Exception as e:
                _logger.error(
                    "Unexpected error syncing channel %s: %s",
                    channel.channel, e)
