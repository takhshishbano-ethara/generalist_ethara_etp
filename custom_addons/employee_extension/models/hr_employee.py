from odoo import fields, models, api
from datetime import datetime


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    offboarding_state = fields.Selection([
        ('active', 'Active'),
        ('offboarding', 'Offboarding'),
        ('offboarded', 'Offboarded'),
    ], string='Offboarding State', default='active', tracking=True)

    offboard_date = fields.Date(string='Offboard Date', readonly=True)
    reason_id = fields.Many2one('hr.employee.offboarding.reasons', string='Offboarding Reasons')
    offboard_notes = fields.Text(string='Offboard Notes')
    is_offboarded = fields.Boolean(string='Is Offboarded', compute='_compute_is_offboarded', store=True)

    @api.depends('offboarding_state')
    def _compute_is_offboarded(self):
        for record in self:
            record.is_offboarded = record.offboarding_state == 'offboarded'

    def action_make_offboard(self):
        self.ensure_one()
        if self.offboarding_state == 'active':
            self.write({
                'offboarding_state': 'offboarding',
                'offboard_date': fields.Date.today(),
            })
        elif self.offboarding_state == 'offboarding':
            self.write({
                'offboarding_state': 'offboarded',
                'active': False,
            })
        return True

    def action_reactivate_employee(self):
        self.ensure_one()
        self.write({
            'offboarding_state': 'active',
            'active': True,
            'offboard_date': False,
        })
        return True

class OffboardingReasons(models.Model):
    _name = 'hr.employee.offboarding.reasons'

    reason = fields.Char(string='Reason')
