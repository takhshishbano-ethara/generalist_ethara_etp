from odoo import fields, models, api

from .assignment_history import ROLE_FIELD_MAP


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    emp_id = fields.Char(
        string='Employee ID',
        copy=False,
        index=True,
        help='Unique identifier for the employee.',
    )

    offboarding_state = fields.Selection([
        ('active', 'Active'),
        ('offboarding', 'Offboarding'),
        ('offboarded', 'Offboarded'),
    ], string='Offboarding State', default='active', tracking=True)

    offboard_date = fields.Date(string='Offboard Date', readonly=True)
    reason_id = fields.Many2one('hr.employee.offboarding.reasons', string='Offboarding Reasons')
    offboard_notes = fields.Text(string='Offboard Notes')
    is_offboarded = fields.Boolean(string='Is Offboarded', compute='_compute_is_offboarded', store=True)

    assignment_history_ids = fields.One2many(
        'hr.employee.assignment.history',
        'employee_id',
        string='Assignment History',
    )

    aadhaar_card_url = fields.Char(string='Aadhaar Card URL')
    resume_url = fields.Char(string='Resume URL')

    @api.depends('offboarding_state')
    def _compute_is_offboarded(self):
        for record in self:
            record.is_offboarded = record.offboarding_state == 'offboarded'

    def _tracked_role_fields(self):
        return {role: fname for role, fname in ROLE_FIELD_MAP.items() if fname in self._fields}

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        role_fields = self._tracked_role_fields()
        if not role_fields:
            return employees
        History = self.env['hr.employee.assignment.history'].sudo()
        now = fields.Datetime.now()
        rows = []
        for emp in employees:
            for role, fname in role_fields.items():
                assignee = emp[fname]
                if assignee:
                    rows.append({
                        'employee_id': emp.id,
                        'role_type': role,
                        'assignee_id': assignee.id,
                        'start_date': now,
                        'changed_by_id': self.env.user.id,
                    })
        if rows:
            History.create(rows)
        return employees

    def write(self, vals):
        role_fields = self._tracked_role_fields()
        tracked = {role: fname for role, fname in role_fields.items() if fname in vals}
        if not tracked:
            return super().write(vals)

        previous = {
            emp.id: {role: emp[fname].id for role, fname in tracked.items()}
            for emp in self
        }
        result = super().write(vals)

        History = self.env['hr.employee.assignment.history'].sudo()
        now = fields.Datetime.now()
        new_rows = []
        for emp in self:
            for role, fname in tracked.items():
                old_id = previous[emp.id][role]
                new_id = emp[fname].id
                if old_id == new_id:
                    continue
                if old_id:
                    open_record = History.search([
                        ('employee_id', '=', emp.id),
                        ('role_type', '=', role),
                        ('assignee_id', '=', old_id),
                        ('end_date', '=', False),
                    ], order='start_date desc', limit=1)
                    if open_record:
                        open_record.write({'end_date': now})
                if new_id:
                    new_rows.append({
                        'employee_id': emp.id,
                        'role_type': role,
                        'from_assignee_id': old_id or False,
                        'assignee_id': new_id,
                        'start_date': now,
                        'changed_by_id': self.env.user.id,
                    })
        if new_rows:
            History.create(new_rows)
        return result


class OffboardingReasons(models.Model):
    _name = 'hr.employee.offboarding.reasons'

    reason = fields.Char(string='Reason')
