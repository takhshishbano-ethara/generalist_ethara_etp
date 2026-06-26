import re

from odoo import _, api, fields, models


EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

ROLE_LABELS = [
    ('tasker', 'Tasker'),
    ('qr', 'Quality Reviewer'),
    ('ql', 'Quality Lead'),
    ('pl', 'Project Lead'),
    ('tpm', 'TPM'),
    ('cto', 'CTO'),
    ('hr_admin', 'HR'),
]

ROLE_API_XMLID = {
    'tasker': 'api_auth_gateway.role_tasker_technical',
    'qr': 'api_auth_gateway.role_qc_non_stem',
    'ql': 'api_auth_gateway.role_qc_technical',
    'pl': 'api_auth_gateway.role_pl_technical',
    'tpm': 'api_auth_gateway.role_tpm_technical',
    'cto': 'api_auth_gateway.role_cto_technical',
    'hr_admin': 'api_auth_gateway.role_hr_technical',
}

ROLE_REQUIRES_MANAGER = {
    'tasker': 'ql',
    'qr': 'pl',
    'ql': 'pl',
    'pl': 'tpm',
}


class EmployeeImportLine(models.TransientModel):
    _name = 'employee.import.line'
    _description = 'Employee Import Preview Line'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'employee.import.wizard', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Name')
    email = fields.Char(string='Email')
    employee_code = fields.Char(string='Employee Code')
    role = fields.Selection(ROLE_LABELS, string='Role')
    reporting_manager_id = fields.Many2one(
        'hr.employee', string='Reporting Manager',
    )
    reporting_manager_email_raw = fields.Char(string='CSV Manager Value')

    requires_manager = fields.Boolean(compute='_compute_requires_manager')
    allowed_manager_role_id = fields.Many2one(
        'api.role', compute='_compute_allowed_manager_role',
    )

    existing_employee_id = fields.Many2one(
        'hr.employee', compute='_compute_existing',
    )
    existing_user_id = fields.Many2one(
        'res.users', compute='_compute_existing',
    )

    status = fields.Selection(
        [('ready', 'Ready'), ('exists', 'Will Update'), ('invalid', 'Invalid')],
        compute='_compute_status',
    )
    has_issues = fields.Boolean(compute='_compute_status')
    issue_text = fields.Text(compute='_compute_status')

    @api.depends('role')
    def _compute_requires_manager(self):
        for line in self:
            line.requires_manager = line.role in ROLE_REQUIRES_MANAGER

    @api.depends('role')
    def _compute_allowed_manager_role(self):
        for line in self:
            parent_role = ROLE_REQUIRES_MANAGER.get(line.role)
            xmlid = ROLE_API_XMLID.get(parent_role) if parent_role else None
            line.allowed_manager_role_id = (
                self.env.ref(xmlid, raise_if_not_found=False) if xmlid else False
            )

    @api.depends('email', 'employee_code')
    def _compute_existing(self):
        Employee = self.env['hr.employee']
        User = self.env['res.users']
        for line in self:
            emp = Employee.browse()
            if line.employee_code:
                emp = Employee.search([('employee_code', '=ilike', line.employee_code)], limit=1)
            if not emp and line.email:
                emp = Employee.search([('work_email', '=ilike', line.email)], limit=1)
            user = User.browse()
            if line.email:
                user = User.search([('login', '=ilike', line.email)], limit=1)
            line.existing_employee_id = emp.id if emp else False
            line.existing_user_id = user.id if user else False

    @api.depends(
        'name', 'email', 'role', 'reporting_manager_id', 'requires_manager',
        'existing_employee_id', 'allowed_manager_role_id',
    )
    def _compute_status(self):
        for line in self:
            issues = []
            if not line.name:
                issues.append(_('Name is required'))
            if not line.email:
                issues.append(_('Email is required'))
            elif not EMAIL_RE.match(line.email):
                issues.append(_('Invalid email format'))
            if not line.role:
                issues.append(_('Role is required'))
            if line.requires_manager and not line.reporting_manager_id:
                parent_key = ROLE_REQUIRES_MANAGER.get(line.role, '')
                issues.append(_(
                    'Reporting manager (%(parent)s) is required for role %(role)s',
                    parent=parent_key.upper(), role=line.role,
                ))
            if line.reporting_manager_id and line.allowed_manager_role_id:
                mgr_user = line.reporting_manager_id.user_id
                if not mgr_user or mgr_user.user_role != line.allowed_manager_role_id:
                    issues.append(_(
                        'Selected manager is not assigned the %(role)s role',
                        role=line.allowed_manager_role_id.name,
                    ))
            line.issue_text = '; '.join(issues) if issues else False
            line.has_issues = bool(issues)
            if issues:
                line.status = 'invalid'
            elif line.existing_employee_id:
                line.status = 'exists'
            else:
                line.status = 'ready'

    @api.onchange('role')
    def _onchange_role_clear_manager(self):
        if self.role and self.role not in ROLE_REQUIRES_MANAGER:
            self.reporting_manager_id = False
