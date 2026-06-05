import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class AssetRequest(models.Model):
    _name = 'asset.request'
    _description = 'Asset Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(
        string='Reference', required=True, readonly=True, copy=False,
        default='New',
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, tracking=True,
        default=lambda self: self.env.user.employee_id,
    )
    project_id = fields.Many2one('project.project', string='Project', tracking=True)
    category_id = fields.Many2one(
        'asset.category', string='Category',
        required=True, tracking=True,
    )
    asset_type = fields.Selection(
        related='category_id.asset_type', store=True, readonly=True,
    )
    preferred_model = fields.Char(string='Preferred Model')
    quantity = fields.Integer(string='Quantity', default=1)
    justification = fields.Text(string='Justification')

    replace_assignment_id = fields.Many2one(
        'asset.assignment',
        string='Replaces Existing Asset',
        domain="[('employee_id', '=', employee_id), ('actual_return_date', '=', False)]",
        help='If filled, on fulfilment this active assignment will be returned automatically.',
    )

    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('submitted', 'Submitted'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('fulfilled', 'Fulfilled'),
        ],
        string='Status', default='draft', tracking=True, required=True,
    )

    requested_by = fields.Many2one(
        'res.users', string='Requested By',
        default=lambda self: self.env.user, readonly=True,
    )
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True)
    approval_date = fields.Datetime(string='Approval Date', readonly=True)
    rejection_reason = fields.Text(string='Rejection Reason')

    asset_id = fields.Many2one(
        'asset.asset', string='Allocated Asset',
        domain="[('state', '=', 'available'), ('category_id', '=', category_id)]",
    )
    assignment_id = fields.Many2one(
        'asset.assignment', string='Created Assignment', readonly=True, copy=False,
    )
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company,
    )

    # ---- Employee snapshot (auto-populated from employee_id) ----------

    employee_work_email = fields.Char(
        related='employee_id.work_email', string='Work Email', readonly=True, store=False,
    )
    employee_work_phone = fields.Char(
        related='employee_id.work_phone', string='Work Phone', readonly=True, store=False,
    )
    department_id = fields.Many2one(
        related='employee_id.department_id', string='Department', readonly=True, store=False,
    )
    job_id = fields.Many2one(
        related='employee_id.job_id', string='Job Title', readonly=True, store=False,
    )
    employee_manager_id = fields.Many2one(
        related='employee_id.parent_id', string='Reporting Manager', readonly=True, store=False,
    )
    employee_active_assignment_ids = fields.Many2many(
        'asset.assignment',
        compute='_compute_employee_assets', string='Currently Held Assets',
    )
    employee_active_assignment_count = fields.Integer(
        compute='_compute_employee_assets', string='# Assets Held',
    )
    employee_active_project_ids = fields.Many2many(
        'project.project',
        compute='_compute_employee_projects', string='Active Projects',
    )

    @api.depends('employee_id')
    def _compute_employee_assets(self):
        Assignment = self.env['asset.assignment'].sudo()
        for rec in self:
            if not rec.employee_id:
                rec.employee_active_assignment_ids = False
                rec.employee_active_assignment_count = 0
                continue
            rows = Assignment.search([
                ('employee_id', '=', rec.employee_id.id),
                ('actual_return_date', '=', False),
            ])
            rec.employee_active_assignment_ids = [(6, 0, rows.ids)]
            rec.employee_active_assignment_count = len(rows)

    @api.depends('employee_id')
    def _compute_employee_projects(self):
        Project = self.env['project.project'].sudo()
        for rec in self:
            if not rec.employee_id:
                rec.employee_active_project_ids = False
                continue
            emp_id = rec.employee_id.id
            domain = ['|', '|',
                      ('project_lead', 'in', emp_id),
                      ('project_qc_reviewer', 'in', emp_id),
                      ('project_tasker', 'in', emp_id)]
            # Restrict to live projects when task_forge_bridge exposes the helper
            if hasattr(Project, '_task_forge_live_domain'):
                try:
                    domain = Project._task_forge_live_domain() + domain
                except Exception:  # noqa: BLE001
                    pass
            projects = Project.search(domain)
            rec.employee_active_project_ids = [(6, 0, projects.ids)]

    # ---- Constraints ---------------------------------------------------

    @api.constrains('quantity')
    def _check_quantity(self):
        for rec in self:
            if rec.quantity is not None and rec.quantity <= 0:
                raise ValidationError(_('Quantity must be greater than zero.'))

    # ---- CRUD ----------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('asset.request') or 'New'
        return super().create(vals_list)

    # ---- Mail helpers --------------------------------------------------

    def _requester_email(self):
        self.ensure_one()
        return (
            self.employee_id.work_email
            or (self.requested_by.email if self.requested_by else False)
            or False
        )

    def _approver_email(self):
        self.ensure_one()
        return (self.approved_by.email if self.approved_by else False) or False

    def _manager_recipient_emails(self):
        """Comma-joined emails of every user in group_asset_manager."""
        group = self.env.ref('asset_management.group_asset_manager', raise_if_not_found=False)
        if not group:
            return ''
        users = self.env['res.users'].sudo().search([('group_ids', 'in', group.id)])
        emails = [u.email for u in users if u.email]
        return ','.join(sorted(set(emails)))

    def _send_template(self, xml_id, email_to=None):
        """Send a mail.template for this record. Logs (does not raise) on failure."""
        self.ensure_one()
        template = self.env.ref(xml_id, raise_if_not_found=False)
        if not template:
            _logger.warning('Mail template %s not found', xml_id)
            return False
        email_values = {'email_to': email_to} if email_to else None
        try:
            template.sudo().send_mail(
                self.id, force_send=True, email_values=email_values,
            )
            return True
        except Exception as e:  # noqa: BLE001 — never block a transition over mail
            _logger.warning(
                'Failed to send %s for request %s: %s', xml_id, self.name, e,
            )
            return False

    # ---- Workflow ------------------------------------------------------

    def _require_state(self, expected):
        for rec in self:
            if rec.state != expected:
                raise UserError(_(
                    'This action is only allowed when status is "%s" (current: %s).'
                ) % (dict(self._fields['state'].selection).get(expected, expected), rec.state))

    def action_submit(self):
        self._require_state('draft')
        self.write({'state': 'submitted'})
        for rec in self:
            rec.message_post(body=_('Request submitted for approval.'))
            rec._send_template(
                'asset_management.email_template_asset_request_submitted_user',
                email_to=rec._requester_email(),
            )
            manager_emails = rec._manager_recipient_emails()
            if manager_emails:
                rec._send_template(
                    'asset_management.email_template_asset_request_submitted_manager',
                    email_to=manager_emails,
                )

    def action_approve(self):
        self._require_state('submitted')
        self.write({
            'state': 'approved',
            'approved_by': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        for rec in self:
            rec.message_post(body=_('Request approved.'))
            rec._send_template(
                'asset_management.email_template_asset_request_approved_user',
                email_to=rec._requester_email(),
            )

    def action_reject(self, reason=None):
        self._require_state('submitted')
        for rec in self:
            final_reason = reason or rec.rejection_reason
            if not final_reason:
                raise UserError(_('A rejection reason is required.'))
            rec.write({'state': 'rejected', 'rejection_reason': final_reason})
            rec.message_post(body=_('Request rejected: %s') % final_reason)
            rec._send_template(
                'asset_management.email_template_asset_request_rejected_user',
                email_to=rec._requester_email(),
            )

    def action_reset_draft(self):
        for rec in self:
            if rec.state != 'rejected':
                raise UserError(_('Only rejected requests can be reset to draft.'))
            rec.write({'state': 'draft', 'rejection_reason': False})
            rec.message_post(body=_('Request reset to draft.'))

    def action_fulfil(self):
        Assignment = self.env['asset.assignment'].sudo()
        for rec in self:
            if rec.state != 'approved':
                raise UserError(_('Only approved requests can be fulfilled.'))
            if not rec.asset_id:
                raise UserError(_('Choose an available asset before fulfilling.'))
            if rec.asset_id.state != 'available':
                raise UserError(_('Asset %s is not available.') % rec.asset_id.display_name)
            if rec.asset_id.category_id.id != rec.category_id.id:
                raise UserError(_('Asset category does not match the request.'))

            today = fields.Date.today()
            expected_return = False
            duration = rec.category_id.default_duration_days or 0
            if duration > 0:
                expected_return = today + timedelta(days=duration)

            assignment = Assignment.create({
                'asset_id': rec.asset_id.id,
                'employee_id': rec.employee_id.id,
                'project_id': rec.project_id.id if rec.project_id else False,
                'request_id': rec.id,
                'assigned_date': today,
                'expected_return_date': expected_return,
                'assigned_by': self.env.user.id,
            })

            rec.asset_id.sudo().write({'state': 'assigned'})

            if rec.replace_assignment_id and rec.replace_assignment_id.is_active:
                rec.replace_assignment_id.sudo().action_return()

            rec.write({'state': 'fulfilled', 'assignment_id': assignment.id})
            rec.message_post(body=_(
                'Request fulfilled: assigned %s to %s.'
            ) % (rec.asset_id.display_name, rec.employee_id.name or ''))
            rec._send_template(
                'asset_management.email_template_asset_request_fulfilled_user',
                email_to=rec._requester_email(),
            )
            rec._send_template(
                'asset_management.email_template_asset_request_fulfilled_manager',
                email_to=rec._approver_email(),
            )
        return True

    # ---- Smart button --------------------------------------------------

    def action_open_assignment(self):
        self.ensure_one()
        if not self.assignment_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'asset.assignment',
            'res_id': self.assignment_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
