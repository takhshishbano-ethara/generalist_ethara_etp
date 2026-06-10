from odoo import models, fields, api
from odoo.exceptions import ValidationError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    task_forge_pl_id = fields.Many2one(
        'hr.employee',
        string='Task Forge PL',
        help='Project Lead this person reports to in Task Forge hierarchy',
    )
    task_forge_qr_id = fields.Many2one(
        'hr.employee',
        string='Task Forge QR',
        help='Quality Reviewer this person reports to in Task Forge hierarchy',
    )
    task_forge_tpm_id = fields.Many2one(
        'hr.employee',
        string='Task Forge TPM',
        help='Technical Program Manager this Project Lead reports to in Task Forge hierarchy',
    )
    tf_allocation_status = fields.Selection(
        [('allocated', 'Allocated'), ('unallocated', 'Unallocated')],
        string='Allocation Status',
        default='unallocated',
    )
    task_forge_active = fields.Boolean(
        string='Task Forge Active',
        default=True,
        help='Soft-delete flag for Task Forge offboarding',
    )
    is_ql_type = fields.Boolean(
        string='Is QL Type',
        default=False,
        help='Check if employee is QL type (QR/QL)',
    )
    tasker_status = fields.Selection(
        [('trainee', 'Trainee'), ('permanent', 'Permanent')],
        string='Tasker Status',
        default='permanent',
    )

    @api.constrains('work_email')
    def _check_ethara_email(self):
        for rec in self:
            if rec.work_email and not rec.work_email.endswith('@ethara.ai'):
                raise ValidationError('Work email must be an @ethara.ai address.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('task_forge_qr_id') and not vals.get('task_forge_pl_id'):
                qr = self.browse(vals['task_forge_qr_id'])
                if qr.task_forge_pl_id:
                    vals['task_forge_pl_id'] = qr.task_forge_pl_id.id
        return super().create(vals_list)

    def _get_task_forge_role(self):
        """Return the Task Forge role string based on user groups."""
        self.ensure_one()
        user = self.user_id
        if not user:
            return 'tasker'
        if user.has_group('etp_user_roles.group_cto') or user.user_role.id == self.env.ref('api_auth_gateway.role_cto_technical').id:
            return 'admin'
        if user.has_group('etp_user_roles.group_tpm') or user.user_role.id == self.env.ref('api_auth_gateway.role_tpm_technical').id:
            return 'tpm'
        if user.has_group('etp_user_roles.group_project_lead') or user.user_role.id in [self.env.ref('api_auth_gateway.role_pl_technical').id, self.env.ref('api_auth_gateway.role_pl_stem').id, self.env.ref('api_auth_gateway.role_pl_non_stem').id]:
            return 'pl'
        if user.has_group('etp_user_roles.group_quality_reviewer') or user.user_role.id in [self.env.ref('api_auth_gateway.role_qc_technical').id, self.env.ref('api_auth_gateway.role_qc_stem').id, self.env.ref('api_auth_gateway.role_qc_non_stem').id]:
            return 'qr'
        if user.has_group('etp_user_roles.group_quality_lead') or (user.user_role.id in [self.env.ref('api_auth_gateway.role_qc_technical').id, self.env.ref('api_auth_gateway.role_qc_stem').id, self.env.ref('api_auth_gateway.role_qc_non_stem').id] and self.is_ql_type):
            return 'ql'
        hr_role = self.env.ref('api_auth_gateway.role_hr_technical', raise_if_not_found=False)
        if hr_role and user.user_role.id == hr_role.id:
            return 'hr'
        return 'tasker'

    def _get_team_employee_ids(self):
        """Return employee IDs visible to this employee based on Task Forge hierarchy."""
        self.ensure_one()
        role = self._get_task_forge_role()
        Employee = self.env['hr.employee'].sudo()
        if role in ('admin', 'hr'):
            return Employee.search([]).ids
        elif role == 'tpm':
            pl_ids = Employee.search([('task_forge_tpm_id', '=', self.id)]).ids
            return Employee.search([
                '|', '|',
                ('id', '=', self.id),
                ('task_forge_tpm_id', '=', self.id),
                ('task_forge_pl_id', 'in', pl_ids),
            ]).ids
        elif role == 'pl':
            return Employee.search([
                '|',
                ('id', '=', self.id),
                '&',
                ('task_forge_pl_id', '=', self.id),
                ('task_forge_active', '=', True),
            ]).ids
        elif role in ('qr', 'ql'):
            return Employee.search([
                '|',
                ('id', '=', self.id),
                '&',
                ('task_forge_qr_id', '=', self.id),
                ('task_forge_active', '=', True),
            ]).ids
        else:
            return [self.id]

    def _get_inactive_team_employee_ids(self):
        self.ensure_one()
        role = self._get_task_forge_role()
        Employee = self.env['hr.employee'].sudo()
        if role == 'admin':
            return Employee.search([('task_forge_active', '=', False)]).ids
        elif role == 'tpm':
            pl_ids = Employee.search([('task_forge_tpm_id', '=', self.id)]).ids
            return Employee.search([
                '|', '|',
                ('task_forge_tpm_id', '=', self.id),
                ('task_forge_pl_id', 'in', pl_ids),
                ('id', '=', self.id),
                ('task_forge_active', '=', False),
            ]).ids
        elif role == 'pl':
            return Employee.search([
                '|',
                ('task_forge_pl_id', '=', self.id),
                ('id', '=', self.id),
                ('task_forge_active', '=', False),
            ]).ids
        elif role in ('qr', 'ql'):
            return Employee.search([
                '|',
                ('task_forge_qr_id', '=', self.id),
                ('id', '=', self.id),
                ('task_forge_active', '=', False),
            ]).ids
        else:
            return [self.id]
