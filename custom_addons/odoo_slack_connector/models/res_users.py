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

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    """Inherited to add Slack user linking fields."""
    _inherit = 'res.users'

    slack_user_ref = fields.Char(
        string="Slack User ID",
        help="Slack member ID linked to this Odoo user",
        readonly=True)
    is_slack_internal_users = fields.Boolean(
        string="Slack User",
        help="True if this user is linked to a Slack account")
    slack_exclude = fields.Boolean(
        string="Exclude from Slack",
        help="If checked, this user will be skipped during Slack sync "
             "and cannot be invited to Slack")

    def action_sync_users(self, slack_members):
        """Match existing Odoo users to Slack members by email.

        Does NOT create new Odoo users. Only sets slack_user_ref on
        existing users whose login (email) matches a Slack member's email.

        Args:
            slack_members (list[dict]): Raw members list from Slack
                users.list API response.

        Returns:
            dict: {matched: int, already_linked: int, unmatched: int,
                   unmatched_emails: list[str]}
        """
        matched = 0
        already_linked = 0
        unmatched_emails = []

        for member in slack_members:
            # Skip deleted/deactivated Slack accounts and bots
            if member.get('deleted') or member.get('is_bot'):
                continue

            profile = member.get('profile', {})
            email = profile.get('email', '').strip().lower()
            if not email:
                continue

            slack_id = member['id']

            # Find existing Odoo user by email (login field)
            odoo_user = self.sudo().search([
                ('login', '=ilike', email),
                ('slack_exclude', '=', False),
            ], limit=1)

            if not odoo_user:
                unmatched_emails.append(email)
                continue

            # Already linked to this Slack ID — skip
            if odoo_user.slack_user_ref == slack_id:
                already_linked += 1
                continue

            # Link the Odoo user to the Slack member
            odoo_user.sudo().write({
                'slack_user_ref': slack_id,
                'is_slack_internal_users': True,
            })
            matched += 1
            _logger.info(
                "Linked Odoo user %s (ID %d) → Slack %s",
                odoo_user.login, odoo_user.id, slack_id)

        _logger.info(
            "User sync: %d matched, %d already linked, %d unmatched",
            matched, already_linked, len(unmatched_emails))

        return {
            'matched': matched,
            'already_linked': already_linked,
            'unmatched': len(unmatched_emails),
            'unmatched_emails': unmatched_emails,
        }

    def invite_to_slack(self):
        """Invite this Odoo user to the Slack workspace.

        Uses the Slack admin.users.invite API to send a workspace
        invitation to the user's email. Requires the bot token to
        have `admin.users:write` scope and belong to an admin/owner.

        Once the user accepts the invite and a sync runs,
        their slack_user_ref will be linked automatically.

        Returns:
            dict: {success: bool, error: str (if failed)}
        """
        self.ensure_one()

        if self.slack_exclude:
            return {
                'success': False,
                'error': 'User "%s" is excluded from Slack '
                         'integration.' % self.name,
            }

        if self.slack_user_ref:
            return {
                'success': False,
                'error': 'User "%s" is already linked to Slack '
                         '(ID: %s).' % (self.name, self.slack_user_ref),
            }

        email = self.login
        if not email or '@' not in email:
            return {
                'success': False,
                'error': 'User "%s" has no valid email address '
                         '(login: %s).' % (self.name, self.login),
            }

        token = self.env.user.company_id.sudo().token
        if not token:
            return {
                'success': False,
                'error': 'Slack Bot Token is not configured. '
                         'Go to Settings → Company → Slack Configuration.',
            }

        headers = {'Authorization': 'Bearer ' + token}
        payload = {
            'email': email,
            'team_id': '',  # Empty = default workspace
            'channels': '',  # No auto-join channels
        }

        try:
            resp = requests.post(
                "https://slack.com/api/admin.users.invite",
                headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.ConnectionError:
            return {
                'success': False,
                'error': 'Cannot reach Slack API. Check internet connection.',
            }
        except requests.exceptions.Timeout:
            return {
                'success': False,
                'error': 'Slack API timed out. Try again.',
            }

        if not result.get('ok'):
            error = result.get('error', 'Unknown')
            # Provide human-readable messages for common errors
            error_messages = {
                'already_invited': 'This user has already been invited '
                                   'to Slack. Ask them to check their email.',
                'already_in_team': 'This user is already a Slack workspace '
                                   'member. Run Sync to link them.',
                'invalid_email': 'Slack rejected the email address: %s' % email,
                'missing_scope': 'Bot token lacks admin.users:write scope. '
                                 'Update your Slack app permissions.',
                'not_admin': 'Bot token must belong to a workspace '
                             'admin/owner to send invitations.',
            }
            friendly_msg = error_messages.get(error, 'Slack API error: %s' % error)
            _logger.warning(
                "Slack invite failed for %s: %s", email, error)
            return {'success': False, 'error': friendly_msg}

        _logger.info("Slack invitation sent to %s (user ID %d)",
                     email, self.id)
        return {
            'success': True,
            'message': 'Invitation sent to %s. Once they accept and '
                       'you run Sync, their Slack account will be '
                       'linked automatically.' % email,
        }
