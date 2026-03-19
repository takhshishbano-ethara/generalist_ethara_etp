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
import re
import requests
from datetime import datetime as dt, timezone
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def remove_html(string):
    regex = re.compile(r'<[^>]+>')
    return regex.sub('', string)


class MailMessage(models.Model):
    """Inherited to add more field and functions"""
    _inherit = 'mail.message'

    is_slack = fields.Boolean(string="Slack",
                              help="True for Slack connected messages")
    channel = fields.Many2one('discuss.channel', string="Slack Channel",
                              help="Channel corresponds to the message")

    @api.model_create_multi
    def create(self, vals_list):
        """Over-riding the create method to sent message to slack"""
        res = super().create(vals_list)
        channels = self.env['discuss.channel'].search([('is_slack', '=', True)])
        for channel in channels:
            msg = self.env['mail.message'].search(
                [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
                 ('is_slack', '=', False)])
            if msg:
                for rec in msg:
                    headers = {
                        'Authorization': 'Bearer ' +
                                         self.env.user.company_id.token
                    }
                    url = ("https://slack.com/api/chat.postMessage?channel=" +
                           rec.record_name + "&" + "text=" + remove_html(
                                rec.body))
                    requests.request("POST", url, headers=headers,
                                     data={})
                    rec.is_slack = True
                channel.msg_date = fields.Datetime.now()
        return res

    def action_synchronization_slack(self):
        """scheduled action to retrieve message from slack"""
        if self.env.company.slack_sync:
            for channel_id in self.env['discuss.channel'].search(
                    [('is_slack', '=', 'true')]):
                headers = {'Authorization': 'Bearer ' +
                                            self.env.user.company_id.token}
                url = ("https://slack.com/api/conversations.history?channel=" +
                       channel_id.channel)
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                channels_history = response.json()
                if channels_history.get('ok'):
                    channels_history['messages'].reverse()
                    if not channel_id.msg_date:
                        channel_id.msg_date = fields.Datetime.now()
                    else:
                        for item in channels_history['messages']:
                            if 'user' in item:
                                users = self.env['res.users'].search(
                                    [('slack_user_ref', '=', item['user'])])
                                # Convert Slack timestamp (unix epoch) to naive UTC datetime
                                msg_datetime = dt.fromtimestamp(
                                    float(item['ts']),
                                    tz=timezone.utc
                                ).replace(tzinfo=None)
                                if msg_datetime > channel_id.msg_date:
                                    self.with_user(users.id).create({
                                        'body': item['text'],
                                        'record_name': channel_id.name,
                                        'model': 'discuss.channel',
                                        'is_slack': True,
                                        'res_id': channel_id.id,
                                    })
                        channel_id.msg_date = fields.Datetime.now()
