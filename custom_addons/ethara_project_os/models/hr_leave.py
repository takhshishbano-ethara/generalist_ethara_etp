"""Leave → roster projection.

The prototype had a real bug here: approving *any* leave marked TODAY as leave, so
approving next month's holiday on Tuesday made the person vanish from Tuesday's roster.

The fix is to let the leave own its own date range and project it onto exactly the days
it covers. Refusing or cancelling an approved leave un-projects those days again — with
one exception, deliberately: a day already locked for payroll is left alone, because a
closed day is a closed day.
"""

from datetime import timedelta

from odoo import api, fields, models


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    def _epo_leave_days(self):
        """Business dates covered by this leave, as a list."""
        self.ensure_one()
        start = self.request_date_from or (self.date_from and self.date_from.date())
        end = self.request_date_to or (self.date_to and self.date_to.date()) or start
        if not start:
            return []
        return [start + timedelta(days=n) for n in range((end - start).days + 1)]

    def _epo_project_to_roster(self):
        """Write 'leave' onto every day this leave covers."""
        Roster = self.env['epo.roster.day'].sudo()
        for leave in self:
            employee = leave.employee_id
            if not employee:
                continue
            today = fields.Date.context_today(leave)
            for day in leave._epo_leave_days():
                # The roster only records today and tomorrow; future leave is applied
                # by the carry-forward cron when those days arrive.
                if day > today + timedelta(days=1):
                    continue
                row = Roster.search([
                    ('employee_id', '=', employee.id), ('business_date', '=', day)], limit=1)
                if row and row.locked:
                    continue
                Roster.upsert(employee.id, day, {
                    'tasking_status': 'leave',
                    'project_id': False,
                    'present': False,
                    'source': 'leave_sync',
                }, source='leave_sync')

    def _epo_unproject_from_roster(self):
        """Undo the projection when an approved leave is refused or cancelled: the day
        goes back to the project it was allocated to, or to the bench."""
        Roster = self.env['epo.roster.day'].sudo()
        for leave in self:
            employee = leave.employee_id
            for day in leave._epo_leave_days():
                row = Roster.search([
                    ('employee_id', '=', employee.id), ('business_date', '=', day),
                    ('tasking_status', '=', 'leave')], limit=1)
                if not row or row.locked:
                    continue
                project = Roster._default_project(employee.id, day)
                row.write({
                    'project_id': project,
                    'tasking_status': 'tasking' if project else 'bench',
                    'source': 'leave_sync',
                })

    def action_validate(self):
        res = super().action_validate()
        self.filtered(lambda l: l.state == 'validate')._epo_project_to_roster()
        return res

    def action_refuse(self):
        approved = self.filtered(lambda l: l.state == 'validate')
        res = super().action_refuse()
        approved._epo_unproject_from_roster()
        return res

    @api.model
    def _cron_epo_sync_roster(self):
        """Catch-up pass: today's approved leave, projected onto today's roster.

        Runs after the carry-forward cron so a leave approved weeks ago still lands on
        the right day without anyone remembering to re-apply it."""
        today = fields.Date.context_today(self)
        leaves = self.sudo().search([
            ('state', '=', 'validate'),
            ('request_date_from', '<=', today),
            ('request_date_to', '>=', today),
        ])
        leaves._epo_project_to_roster()
        return len(leaves)
