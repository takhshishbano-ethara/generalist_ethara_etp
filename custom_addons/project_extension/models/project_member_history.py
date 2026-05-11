from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)

ROLE_FIELD_MAP = {
    'project_lead': 'lead',
    'project_qc_reviewer': 'qc_reviewer',
    'project_tasker': 'tasker',
    'project_aire': 'aire',
    'project_swe': 'swe',
}


class ProjectMemberHistory(models.Model):
    _name = 'project.member.history'
    _description = 'Project Member Assignment History'
    _order = 'start_date desc, id desc'

    project_id = fields.Many2one('project.project', string='Project', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, ondelete='cascade', index=True)
    role = fields.Selection([
        ('lead', 'Project Lead'),
        ('qc_reviewer', 'QC Reviewer'),
        ('tasker', 'Tasker'),
        ('aire', 'AI Research Engineer'),
        ('swe', 'Software Engineer'),
    ], string='Role', required=True)
    start_date = fields.Date(string='Start Date', default=fields.Date.today)
    end_date = fields.Date(string='End Date')
    state = fields.Selection([
        ('active', 'Active'),
        ('offboarded', 'Offboarded'),
    ], string='Status', default='active')
    offboard_reason = fields.Text(string='Offboard Reason')
    notes = fields.Text(string='Notes')
    reason_id = fields.Many2one('hr.employee.offboarding.reasons', string='Offboarding Reasons')

    def action_offboard(self, reason=None, notes=None, end_date=None):
        self.write({
            'state': 'offboarded',
            'end_date': end_date or fields.Date.today(),
            'offboard_reason': reason or '',
            'notes': notes or '',
        })
