from odoo import fields, models, api
from odoo.exceptions import ValidationError


class EmployeeAllocationRequest(models.Model):
    _name = 'employee.allocation.request'
    _description = 'Employee Allocation Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', required=True, copy=False, readonly=True, default=lambda self: self.env['ir.sequence'].next_by_code('employee.allocation.request'))

    project_id = fields.Many2one('project.project', string='Project', required=True, tracking=True)

    role_id = fields.Many2one('api.role', string='Requested Role', required=True, tracking=True)

    quantity = fields.Integer(string='Number of Employees Required', required=True, default=1)

    justification = fields.Text(string='Justification', required=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='Status', default='draft', tracking=True)

    requested_by = fields.Many2one('res.users', string='Requested By', 
                                   default=lambda self: self.env.user, readonly=True)

    approval_date = fields.Datetime(string='Approval Date', readonly=True)

    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True)
    assign_employees = fields.Many2many('hr.employee', 'emp_allocation_request_hr_employee_rel',
                                    string='Assign Employee')

    notes = fields.Text(string='Notes')

    @api.constrains('quantity')
    def _check_quantity(self):
        for record in self:
            if record.quantity <= 0:
                raise ValidationError('The quantity must be greater than 0.')

    @api.onchange('requested_by')
    def _onchange_requested_by(self):
        if self.requested_by and self.requested_by.user_role_id:
            self.role_id = self.requested_by.user_role_id

    def action_submit(self):
        self.ensure_one()
        self.write({'state': 'submitted'})

    def action_approve(self):
        self.ensure_one()
        self.write({
            'state': 'approved',
            'approval_date': fields.Datetime.now(),
            'approved_by': self.env.user.id,
        })

    def action_reject(self):
        self.ensure_one()
        self.write({'state': 'rejected'})

    def action_reset_draft(self):
        self.ensure_one()
        self.write({'state': 'draft'})

    @api.model
    def search_read_active_employees(self):
        employees = self.env['hr.employee'].search([
            ('offboarding_state', '=', 'active'),
            ('active', '=', True),
        ])
        return employees.read(['name', 'job_title', 'department_id'])

    @api.model
    def create(self, vals):
        if vals.get('requested_by'):
            user = self.env['res.users'].browse(vals.get('requested_by'))
            if user.user_role_id and not vals.get('role_id'):
                vals['role_id'] = user.user_role_id.id
        return super().create(vals)

    def write(self, vals):
        if vals.get('requested_by'):
            user = self.env['res.users'].browse(vals.get('requested_by'))
            if user.user_role_id and not vals.get('role_id'):
                vals['role_id'] = user.user_role_id.id
        return super().write(vals)