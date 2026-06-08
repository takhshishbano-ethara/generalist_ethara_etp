# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Unnimaya C O (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
################################################################################
import logging
import requests
from odoo import fields, models
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """Inherited to add more field and functions"""
    _inherit = 'res.company'

    token = fields.Char(string="Slack Token",
                        help="Token for connecting with Slack")
    slack_users_ids = fields.One2many('slack.user',
                                      'res_company_id',
                                      string="All Users",
                                      help="All Slack Users")
    slack_channel_ids = fields.One2many('slack.channel',
                                        'res_company_id',
                                        string="Channels",
                                        help="All Slack channels")
    slack_sync = fields.Boolean(string="Is Slack",
                                help="True if connected to Slack")

    # Last sync result fields (read-only display)
    slack_last_sync = fields.Datetime(
        string="Last Sync", readonly=True,
        help="Timestamp of the last successful Slack sync")
    slack_sync_matched = fields.Integer(
        string="Users Matched", readonly=True,
        help="Odoo users matched to Slack members in last sync")
    slack_sync_already_linked = fields.Integer(
        string="Already Linked", readonly=True,
        help="Users already linked from a previous sync")
    slack_sync_unmatched = fields.Integer(
        string="Unmatched Slack Members", readonly=True,
        help="Slack members with no matching Odoo user")

    def _slack_api_get(self, url):
        """GET from Slack API with auth, timeout, and error handling.

        Returns:
            dict: Parsed JSON response from Slack.
        Raises:
            UserError: On network or Slack API errors.
        """
        headers = {'Authorization': 'Bearer ' + self.token}
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise UserError(
                "Cannot reach Slack API. Check your internet connection.")
        except requests.exceptions.Timeout:
            raise UserError("Slack API timed out. Try again later.")
        data = resp.json()
        if not data.get('ok'):
            raise UserError(
                "Slack API error: %s" % data.get('error', 'Unknown'))
        return data

    def action_sync(self):
        """Sync Slack workspace with Odoo.

        New flow (no user/contact creation):
          1. Validate token & connectivity
          2. Fetch Slack members → match to existing Odoo users by email
          3. Fetch Slack channels → create missing discuss.channel records
          4. Sync channel membership (existing users only)
          5. Update slack.user / slack.channel lists on Company tab
        """
        if not self.token:
            raise UserError("Please set a Slack Bot Token before syncing.")

        # Connectivity pre-check
        try:
            requests.get("https://slack.com/api/api.test", timeout=5)
        except requests.exceptions.ConnectionError:
            raise UserError(
                "Cannot reach Slack API. Please check your internet "
                "connection.")
        except requests.exceptions.Timeout:
            raise UserError(
                "Slack API is not responding. Please try again later.")

        self.slack_sync = True

        # --- Step 1: Match Slack members to existing Odoo users ---
        users_data = self._slack_api_get("https://slack.com/api/users.list")
        members = users_data.get('members', [])
        sync_result = self.env['res.users'].action_sync_users(members)

        # --- Step 2: Sync Slack channels → discuss.channel ---
        channels_data = self._slack_api_get(
            "https://slack.com/api/conversations.list")
        slack_channels = channels_data.get('channels', [])

        existing_refs = self.env['discuss.channel'].search([
            ('is_slack', '=', True)
        ]).mapped('channel')
        for ch in slack_channels:
            if ch['id'] not in existing_refs:
                self.env['discuss.channel'].create({
                    'name': ch['name'],
                    'channel': ch['id'],
                    'is_slack': True,
                })

        # --- Step 3: Sync channel members (existing users only) ---
        self.env['discuss.channel'].search([
            ('is_slack', '=', True)
        ]).action_sync_members()

        # --- Step 4: Update Company tab read-only lists ---
        # Slack users directory
        existing_slack_user_ids = self.slack_users_ids.mapped('user')
        users_list = []
        for rec in members:
            if rec.get('deleted'):
                continue
            if rec['id'] not in existing_slack_user_ids:
                email = rec.get('profile', {}).get('email', '')
                users_list.append((0, 0, {
                    'name': rec.get('real_name', rec.get('name', '')),
                    'user': rec['id'],
                    'email': email,
                }))
        if users_list:
            self.slack_users_ids = users_list

        # Slack channels directory
        existing_channel_names = self.slack_channel_ids.mapped('name')
        channels_list = []
        for ch in slack_channels:
            if ch['name'] not in existing_channel_names:
                channels_list.append((0, 0, {'name': ch['name']}))
        if channels_list:
            self.slack_channel_ids = channels_list

        logger.info(
            "Slack sync complete: %d matched, %d unmatched, %d channels",
            sync_result.get('matched', 0),
            sync_result.get('unmatched', 0),
            len(slack_channels),
        )

        # Store sync results for display in Company form
        self.write({
            'slack_last_sync': fields.Datetime.now(),
            'slack_sync_matched': sync_result.get('matched', 0),
            'slack_sync_already_linked': sync_result.get(
                'already_linked', 0),
            'slack_sync_unmatched': sync_result.get('unmatched', 0),
        })
