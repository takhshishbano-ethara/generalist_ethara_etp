from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .role_map import resolve_role_ids


PROJECT_STATE_START = 'start'
PROJECT_STATE_PAUSE = 'pause'
PROJECT_STATE_CLOSE = 'close'
PROJECT_STATE_COMPLETE = 'complete'

PROJECT_STATE_SELECTION = [
    (PROJECT_STATE_START, 'Started'),
    (PROJECT_STATE_PAUSE, 'Paused'),
    (PROJECT_STATE_CLOSE, 'Closed'),
    (PROJECT_STATE_COMPLETE, 'Completed'),
]

VALID_PROJECT_STATES = tuple(k for k, _label in PROJECT_STATE_SELECTION)

TEAM_FIELDS = ('assigned_tpm_ids', 'assigned_pl_ql_ids', 'assigned_rnd_ids')


class EtharaProject(models.Model):
    _name = 'ethara.project'
    _description = 'Ethara Project'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Project Name',
        required=True,
        tracking=True,
    )
    client_name = fields.Char(
        string='Client Name',
        required=True,
        tracking=True,
    )
    internal_project_name = fields.Char(
        string='Internal Project Name',
        tracking=True,
    )
    project_goal = fields.Text(
        string='Project Goal',
    )
    start_date = fields.Date(
        string='Start Date',
        tracking=True,
    )
    end_date = fields.Date(
        string='End Date',
        tracking=True,
    )

    state = fields.Selection(
        selection=PROJECT_STATE_SELECTION,
        string='Status',
        default=PROJECT_STATE_START,
        required=True,
        tracking=True,
        index=True,
        copy=False,
    )

    attachment_ids = fields.One2many(
        comodel_name='ethara.project.attachment',
        inverse_name='project_id',
        string='Attachments',
        copy=True,
    )

    assigned_tpm_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_tpm_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned TPM',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'tpm'))],
        help='Employees whose user_role is the TPM api.role.',
    )
    assigned_pl_ql_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_pl_ql_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned PL / QL',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'pl_ql'))],
        help='Employees whose user_role is one of the PL / QC / QR api.role records.',
    )
    assigned_rnd_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_rnd_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned R&D',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'rnd'))],
        help='Employees whose user_role is the R&D api.role.',
    )

    attachment_count = fields.Integer(
        string='Attachment Count',
        compute='_compute_attachment_count',
    )

    @api.depends('attachment_ids')
    def _compute_attachment_count(self):
        for rec in self:
            rec.attachment_count = len(rec.attachment_ids)

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.start_date > rec.end_date:
                raise ValidationError(_('Start date must be on or before end date.'))

    def _collect_team_employee_ids(self):
        ids = set()
        for rec in self:
            for f in TEAM_FIELDS:
                ids |= set(rec[f].ids)
        return ids

    def _recompute_team_work_status(self, extra_ids=None):
        emp_ids = self._collect_team_employee_ids()
        if extra_ids:
            emp_ids |= set(extra_ids)
        if not emp_ids:
            return
        self.env['hr.employee'].sudo().browse(list(emp_ids))._recompute_ethara_work_status()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._recompute_team_work_status()
        return records

    def write(self, vals):
        touches_status = 'state' in vals or any(f in vals for f in TEAM_FIELDS)
        old_ids = self._collect_team_employee_ids() if touches_status else set()
        result = super().write(vals)
        if touches_status:
            self._recompute_team_work_status(extra_ids=old_ids)
        return result

    def action_set_state(self, new_state):
        if new_state not in VALID_PROJECT_STATES:
            raise ValidationError(_(
                "Invalid project state '%s'. Allowed: %s"
            ) % (new_state, ', '.join(VALID_PROJECT_STATES)))
        self.write({'state': new_state})
        return True

    def action_start(self):
        return self.action_set_state(PROJECT_STATE_START)

    def action_pause(self):
        return self.action_set_state(PROJECT_STATE_PAUSE)

    def action_close(self):
        return self.action_set_state(PROJECT_STATE_CLOSE)

    def action_complete(self):
        return self.action_set_state(PROJECT_STATE_COMPLETE)
