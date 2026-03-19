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
import json
import requests
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class CalendarEvent(models.Model):
    _inherit = 'calendar.event'

    is_google_meet = fields.Boolean('Google Meet', default=False,
                                    help="Boolean field indicating if the "
                                         "event has a Google Meet integration.")
    google_meet_url = fields.Char('Google Meet URL',
                                  help='Joinging Meeting URL')
    google_meet_code = fields.Char('Google Meet Code',
                                   help='Joining Meeting Code')
    google_event_id = fields.Char('Google Event ID',
                                  help='Event ID of the google meet')

    def _get_header(self):
        return {
            'Authorization': f'Bearer {self.env.company.hangout_company_access_token}',
            'Content-Type': 'application/json'
        }

    def _create_google_meet(self):
        self.ensure_one()
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?conferenceDataVersion=1"
        event_data = {
            'summary': self.name,
            'start': {'dateTime': self.start.isoformat() + 'Z'},
            'end': {'dateTime': self.stop.isoformat() + 'Z'},
            'conferenceData': {
                "createRequest": {"conferenceSolutionKey": {"type": "hangoutsMeet"}, "requestId": f"meet_{self.id}"}},
        }
        res = requests.post(url, headers=self._get_header(), json=event_data, timeout=20)
        if res.status_code == 401:
            self.env.company.google_meet_company_refresh_token()
            res = requests.post(url, headers=self._get_header(), json=event_data, timeout=20)

        result = res.json()
        if result.get('hangoutLink'):
            self.write({
                'google_event_id': result.get('id'),
                'google_meet_url': result.get('hangoutLink'),
                'google_meet_code': result.get('conferenceData', {}).get('conferenceId'),
            })

    def _delete_google_meet(self, g_id):
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{g_id}"
        requests.delete(url, headers=self._get_header(), timeout=20)

    @api.model_create_multi
    def create(self, vals_list):
        events = super().create(vals_list)
        for event in events:
            if event.is_google_meet: event._create_google_meet()
        return events

    def write(self, vals):
        if 'is_google_meet' in vals and not vals['is_google_meet']:
            for event in self:
                if event.google_event_id: event._delete_google_meet(event.google_event_id)
            vals.update({'google_meet_url': False, 'google_meet_code': False, 'google_event_id': False})
        res = super().write(vals)
        if vals.get('is_google_meet'):
            for event in self:
                if not event.google_event_id: event._create_google_meet()
        return res

    def unlink(self):
        for event in self:
            if event.google_event_id: event._delete_google_meet(event.google_event_id)
        return super().unlink()
