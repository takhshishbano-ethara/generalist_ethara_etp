from odoo import fields, models, api


ROLE_FIELD_MAP = {
    'qr': 'task_forge_qr_id',
    'pl': 'task_forge_pl_id',
    'tpm': 'task_forge_tpm_id',
}


class HrEmployeeAssignmentHistory(models.Model):
    _name = 'hr.employee.assignment.history'
    _description = 'Employee QR/PL/TPM Assignment History'
    _order = 'start_date desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
        required=True,
        ondelete='cascade',
        index=True,
    )
    role_type = fields.Selection(
        [('qr', 'QR'), ('pl', 'PL'), ('tpm', 'TPM')],
        string='Role',
        required=True,
        index=True,
    )
    from_assignee_id = fields.Many2one(
        'hr.employee',
        string='Assigned From',
        ondelete='restrict',
        help='Previous QR/PL/TPM, empty for the initial assignment.',
    )
    assignee_id = fields.Many2one(
        'hr.employee',
        string='Assigned To',
        required=True,
        ondelete='restrict',
    )
    start_date = fields.Datetime(
        string='Started On',
        required=True,
        default=fields.Datetime.now,
    )
    end_date = fields.Datetime(
        string='Released On',
        help='Empty while the assignment is currently active.',
    )
    is_active = fields.Boolean(
        string='Currently Active',
        compute='_compute_is_active',
        store=True,
    )
    changed_by_id = fields.Many2one(
        'res.users',
        string='Changed By',
        default=lambda self: self.env.user,
        readonly=True,
    )
    duration_days = fields.Integer(
        string='Duration (days)',
        compute='_compute_duration_days',
    )

    @api.depends('end_date')
    def _compute_is_active(self):
        for rec in self:
            rec.is_active = not rec.end_date

    @api.depends('start_date', 'end_date')
    def _compute_duration_days(self):
        for rec in self:
            if not rec.start_date:
                rec.duration_days = 0
                continue
            end = rec.end_date or fields.Datetime.now()
            rec.duration_days = (end - rec.start_date).days
