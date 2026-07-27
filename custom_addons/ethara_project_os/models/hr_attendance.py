"""Attendance → roster presence.

Presence on the roster is not typed in by a lead; it follows the check-in. The roster
row is created if the person has none yet for today, so the first check-in of the
morning is enough to put someone on the board.
"""

from odoo import api, fields, models


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._epo_mark_present()
        return records

    def _epo_mark_present(self):
        Roster = self.env['epo.roster.day'].sudo()
        for att in self:
            if not att.employee_id or not att.check_in:
                continue
            day = fields.Date.context_today(att, att.check_in)
            row = Roster.search([
                ('employee_id', '=', att.employee_id.id),
                ('business_date', '=', day)], limit=1)
            if row:
                if not row.locked and not row.present:
                    row.write({'present': True})
                continue
            project = Roster._default_project(att.employee_id.id, day)
            Roster.create({
                'employee_id': att.employee_id.id,
                'business_date': day,
                'project_id': project,
                'tasking_status': 'tasking' if project else 'bench',
                'present': True,
                'source': 'attendance',
            })
