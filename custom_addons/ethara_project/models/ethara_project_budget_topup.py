import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


TOPUP_STATE_SELECTION = [
    ('draft', 'Draft'),
    ('pending', 'Pending Approval'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected'),
]

TEMPLATE_TOPUP_SUBMITTED = 'ethara_project.mail_template_ethara_budget_topup_submitted'
TEMPLATE_TOPUP_APPROVED = 'ethara_project.mail_template_ethara_budget_topup_approved'
TEMPLATE_TOPUP_REJECTED = 'ethara_project.mail_template_ethara_budget_topup_rejected'


class EtharaProjectBudgetTopup(models.Model):
    _name = 'ethara.project.budget.topup'
    _description = 'Ethara Project Budget Top-up'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        copy=False,
        readonly=True,
        default='New',
        tracking=True,
    )
    budget_id = fields.Many2one(
        comodel_name='ethara.project.budget',
        string='Project Budget',
        required=True,
        ondelete='cascade',
        index=True,
        tracking=True,
    )
    ethara_project_id = fields.Many2one(
        related='budget_id.ethara_project_id',
        store=True,
        readonly=True,
        index=True,
    )
    amount = fields.Float(
        string='Top-up Amount (USD)',
        required=True,
        tracking=True,
    )
    reason_id = fields.Many2one(
        comodel_name='ethara.project.phase.topup.reason',
        string='Reason',
        ondelete='restrict',
    )
    justification = fields.Text(string='Justification', required=True)
    state = fields.Selection(
        selection=TOPUP_STATE_SELECTION,
        string='Status',
        default='draft',
        required=True,
        tracking=True,
        copy=False,
    )
    requester_id = fields.Many2one(
        comodel_name='res.users',
        string='Requester',
        default=lambda self: self.env.user,
        tracking=True,
    )
    approver_id = fields.Many2one(
        comodel_name='res.users',
        string='Approver',
        readonly=True,
        tracking=True,
    )
    approval_date = fields.Datetime(readonly=True, tracking=True)
    rejection_reason = fields.Text(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('ethara.project.budget.topup')
                    or 'New'
                )
        return super().create(vals_list)

    def _check_can_approve(self):
        self.ensure_one()
        if self.env.user not in self.budget_id.approver_user_ids:
            raise UserError(_(
                'You are not in the approver pool for this Project Budget.'
            ))

    def _requester_partner_ids(self):
        self.ensure_one()
        return self.requester_id.partner_id.ids if self.requester_id else []

    def _approver_partner_ids(self):
        self.ensure_one()
        return self.budget_id.approver_user_ids.mapped('partner_id').ids

    def _send_thread_mail(self, template_xmlid, partner_ids, email_values=None):
        self.ensure_one()
        project = self.ethara_project_id
        if not project or not partner_ids:
            return
        try:
            project._ethara_post_thread_message(
                template_xmlid, self, partner_ids, email_values=email_values,
            )
        except Exception:
            _logger.exception(
                'Failed to thread top-up mail for %s', self.id,
            )

    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only draft top-ups can be submitted.'))
            if (rec.amount or 0.0) <= 0.0:
                raise UserError(_('Top-up amount must be greater than zero.'))
            if not rec.budget_id.approver_user_ids:
                raise UserError(_(
                    'Project Budget has no approvers configured.'
                ))
            rec.state = 'pending'
            rec._send_thread_mail(
                TEMPLATE_TOPUP_SUBMITTED,
                rec._approver_partner_ids(),
            )

    def action_approve(self):
        for rec in self:
            if rec.state != 'pending':
                raise UserError(_('Only pending top-ups can be approved.'))
            rec._check_can_approve()
            rec.write({
                'state': 'approved',
                'approver_id': self.env.user.id,
                'approval_date': fields.Datetime.now(),
            })
            if rec.budget_id:
                rec.budget_id.budget_amount = (
                    (rec.budget_id.budget_amount or 0.0) + (rec.amount or 0.0)
                )
            rec._send_thread_mail(
                TEMPLATE_TOPUP_APPROVED,
                rec._requester_partner_ids(),
            )

    def action_reject(self):
        self.ensure_one()
        if self.state != 'pending':
            raise UserError(_('Only pending top-ups can be rejected.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reject Top-up'),
            'res_model': 'ethara.project.budget.topup.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_topup_id': self.id},
        }

    def _do_reject(self, reason):
        self.ensure_one()
        if self.state != 'pending':
            raise UserError(_('Only pending top-ups can be rejected.'))
        self._check_can_approve()
        self.write({
            'state': 'rejected',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
            'rejection_reason': reason,
        })
        self._send_thread_mail(
            TEMPLATE_TOPUP_REJECTED,
            self._requester_partner_ids(),
        )

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state != 'rejected':
                raise UserError(_('Only rejected top-ups can be reset.'))
            rec.write({
                'state': 'draft',
                'approver_id': False,
                'approval_date': False,
                'rejection_reason': False,
            })
