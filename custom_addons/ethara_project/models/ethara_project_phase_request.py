import logging
import threading
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

from .role_map import ROLE_XML_IDS

_logger = logging.getLogger(__name__)


REQUEST_STATE_SELECTION = [
    ('draft', 'Draft'),
    ('cto_review', 'CTO Review'),
    ('cfo_review', 'CFO Approval'),
    ('changes_required', 'Changes Required'),
    ('approved', 'Approved'),
    ('partially_approved', 'Partially Approved'),
    ('withdrawn', 'Withdrawn'),
]

TERMINAL_APPROVED_STATES = ('approved', 'partially_approved')

PL_TPM_ROLE_XMLIDS = ROLE_XML_IDS['pl'] + ROLE_XML_IDS['tpm']
# Roles allowed to raise (submit) a budget request. PL/TPM plus R&D members;
# CTO/CFO remain approvers/rejecters only.
RAISER_ROLE_XMLIDS = ROLE_XML_IDS['pl'] + ROLE_XML_IDS['tpm'] + ROLE_XML_IDS['rnd']
CTO_ROLE_XMLIDS = ROLE_XML_IDS['cto']
CFO_ROLE_XMLIDS = ROLE_XML_IDS['cfo']

TEMPLATE_SUBMITTED = 'ethara_project.mail_template_ethara_phase_request_submitted'
TEMPLATE_CTO_REVIEW = 'ethara_project.mail_template_ethara_phase_request_cto_review'
TEMPLATE_CFO_REVIEW = 'ethara_project.mail_template_ethara_phase_request_cfo_review'
TEMPLATE_APPROVED = 'ethara_project.mail_template_ethara_phase_request_approved'
TEMPLATE_CTO_REJECTED = 'ethara_project.mail_template_ethara_phase_request_cto_rejected'
TEMPLATE_CFO_CHANGES = 'ethara_project.mail_template_ethara_phase_request_cfo_changes_required'


class EtharaProjectPhaseRequest(models.Model):
    _name = 'ethara.project.phase.request'
    _description = 'Ethara Project Phase Budget Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'request_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        copy=False,
        readonly=True,
        default='New',
        tracking=True,
    )
    phase_id = fields.Many2one(
        comodel_name='ethara.project.phase',
        string='Phase',
        required=True,
        ondelete='cascade',
        index=True,
        tracking=True,
    )
    budget_id = fields.Many2one(
        related='phase_id.budget_id',
        store=True,
        readonly=True,
    )
    ethara_project_id = fields.Many2one(
        related='phase_id.ethara_project_id',
        store=True,
        readonly=True,
    )
    sequence_number = fields.Integer(
        string='Request #',
        compute='_compute_sequence_number',
        store=True,
        help="Position of this request among the phase's requests.",
    )
    request_date = fields.Datetime(
        string='Requested On',
        default=fields.Datetime.now,
        required=True,
        tracking=True,
    )
    state = fields.Selection(
        selection=REQUEST_STATE_SELECTION,
        string='Status',
        default='draft',
        required=True,
        tracking=True,
        copy=False,
    )
    request_type = fields.Selection(
        selection=[
            ('budget', 'Budget'),
            ('new_model', 'New Model'),
            ('topup', 'Top-up'),
            ('device', 'Device'),
        ],
        string='Request Type',
        default='budget',
        required=True,
        tracking=True,
    )
    revision_no = fields.Integer(
        string='Revision',
        default=0,
        readonly=True,
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
    rejection_reason = fields.Text(readonly=True, tracking=True)
    cto_reviewer_id = fields.Many2one(
        comodel_name='res.users',
        string='CTO Reviewer',
        readonly=True,
        tracking=True,
        copy=False,
    )
    cto_review_date = fields.Datetime(
        string='CTO Reviewed On',
        readonly=True,
        tracking=True,
        copy=False,
    )
    cto_review_note = fields.Text(
        string='CTO Review Note',
        readonly=True,
        copy=False,
        tracking=True,
    )
    cfo_approver_id = fields.Many2one(
        comodel_name='res.users',
        string='CFO Approver',
        readonly=True,
        tracking=True,
        copy=False,
    )
    cfo_approval_date = fields.Datetime(
        string='CFO Decision On',
        readonly=True,
        tracking=True,
        copy=False,
    )
    cfo_change_request_note = fields.Text(
        string='CFO Change Request Note',
        readonly=True,
        copy=False,
        tracking=True,
    )
    rejected_by = fields.Many2one(
        comodel_name='res.users',
        string='Rejected By',
        readonly=True,
        copy=False,
        tracking=True,
    )
    justification = fields.Text(string='Justification')
    subject = fields.Char(
        string='Email Subject',
        help='Optional. Overrides the default approval email subject.',
    )
    message = fields.Html(
        string='Message',
        help='Optional note included in the approval email body.',
    )
    priority = fields.Selection(
        selection=[
            ('low', 'Low'),
            ('normal', 'Normal'),
            ('high', 'High'),
            ('urgent', 'Urgent'),
        ],
        string='Priority',
        default='normal',
        tracking=True,
    )
    attachment_ids = fields.Many2many(
        comodel_name='ir.attachment',
        relation='ethara_project_phase_request_attachment_rel',
        column1='request_id',
        column2='attachment_id',
        string='Attachments',
    )
    attachment_urls = fields.Text(
        string='Attachment URLs',
        help='CSV of S3 URLs propagated from the parent project budget.',
    )
    topup_reason_id = fields.Many2one(
        comodel_name='ethara.project.phase.topup.reason',
        string='Top-up Reason',
        ondelete='restrict',
    )
    total_tasks = fields.Integer(string='Total Tasks')
    buffer_pct = fields.Float(string='Buffer %', default=0.0)
    model_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.request.model.line',
        inverse_name='request_id',
        string='Model Lines',
        copy=True,
    )
    infra_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.request.infra.line',
        inverse_name='request_id',
        string='Infrastructure Lines',
        copy=True,
    )
    subscription_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.request.subscription.line',
        inverse_name='request_id',
        string='Subscription Lines',
        copy=True,
    )
    requested_total = fields.Float(string='Requested Total (USD)')
    approved_total = fields.Float(string='Approved Total (USD)')
    remaining_amount = fields.Float(
        string='Remaining (USD)',
        compute='_compute_remaining_amount',
    )
    active = fields.Boolean(default=True)

    is_current_user_pl_or_tpm = fields.Boolean(
        compute='_compute_current_user_roles',
    )
    is_current_user_cto = fields.Boolean(
        compute='_compute_current_user_roles',
    )
    is_current_user_cfo = fields.Boolean(
        compute='_compute_current_user_roles',
    )

    @api.depends_context('uid')
    def _compute_current_user_roles(self):
        is_pl = self._user_has_role(PL_TPM_ROLE_XMLIDS)
        is_cto = self._user_has_role(CTO_ROLE_XMLIDS)
        is_cfo = self._user_has_role(CFO_ROLE_XMLIDS)
        for rec in self:
            rec.is_current_user_pl_or_tpm = is_pl
            rec.is_current_user_cto = is_cto
            rec.is_current_user_cfo = is_cfo

    @api.depends(
        'phase_id',
        'phase_id.request_ids',
        'phase_id.request_ids.request_date',
    )
    def _compute_sequence_number(self):
        for rec in self:
            if not rec.phase_id:
                rec.sequence_number = 0
                continue
            ordered = rec.phase_id.request_ids.sorted(
                key=lambda r: (r.request_date or fields.Datetime.now(), r.id)
            )
            pos = 0
            for idx, req in enumerate(ordered, start=1):
                if req == rec:
                    pos = idx
                    break
            rec.sequence_number = pos

    @api.depends('requested_total', 'approved_total')
    def _compute_remaining_amount(self):
        for rec in self:
            rec.remaining_amount = max(
                0.0,
                (rec.requested_total or 0.0) - (rec.approved_total or 0.0),
            )

    @api.onchange('total_tasks')
    def _onchange_total_tasks_update_lines(self):
        for rec in self:
            for line in rec.model_line_ids:
                line.requested_amount = (
                    (rec.total_tasks or 0) * (line.per_task_cost or 0.0)
                )

    @api.onchange(
        'model_line_ids',
        'infra_line_ids',
        'subscription_line_ids',
        'buffer_pct',
    )
    def _onchange_suggest_totals(self):
        for rec in self:
            factor = 1.0 + ((rec.buffer_pct or 0.0) / 100.0)
            sub_requested = sum(
                (line.requested_amount or line.final_amount or 0.0)
                for line in rec.subscription_line_ids
            )
            sub_approved = sum(
                (line.approved_amount or 0.0)
                for line in rec.subscription_line_ids
            )
            requested_base = (
                sum(rec.model_line_ids.mapped('requested_amount'))
                + sum(rec.infra_line_ids.mapped('requested_amount'))
                + sub_requested
            )
            approved_base = (
                sum(rec.model_line_ids.mapped('approved_amount'))
                + sum(rec.infra_line_ids.mapped('approved_amount'))
                + sub_approved
            )
            rec.requested_total = requested_base * factor
            rec.approved_total = approved_base * factor

    @api.model
    def _resolve_role_ids(self, xmlids):
        env = self.env
        ids = []
        for xmlid in xmlids:
            rec = env.ref(xmlid, raise_if_not_found=False)
            if rec:
                ids.append(rec.id)
        return ids

    @api.model
    def _user_has_role(self, xmlids):
        user_role = getattr(self.env.user, 'user_role', False)
        if not user_role:
            return False
        return user_role.id in self._resolve_role_ids(xmlids)

    def _is_pl_or_tpm(self):
        return self._user_has_role(PL_TPM_ROLE_XMLIDS)

    def _is_cto(self):
        return self._user_has_role(CTO_ROLE_XMLIDS)

    def _is_cfo(self):
        return self._user_has_role(CFO_ROLE_XMLIDS)

    def _users_with_role(self, xmlids):
        role_ids = self._resolve_role_ids(xmlids)
        if not role_ids:
            return self.env['res.users']
        return self.env['res.users'].search([('user_role', 'in', role_ids)])

    def _distribute_approved_amount(self):
        self.ensure_one()
        approved_total = self.approved_total or 0.0

        sub_targets = {
            line.id: (line.requested_amount or line.final_amount or 0.0)
            for line in self.subscription_line_ids
        }
        infra_targets = {
            line.id: (line.requested_amount or 0.0)
            for line in self.infra_line_ids
        }
        model_targets = {
            line.id: (line.requested_amount or 0.0)
            for line in self.model_line_ids
        }
        sub_total = sum(sub_targets.values())
        infra_total = sum(infra_targets.values())
        model_total = sum(model_targets.values())

        def _zero(lines):
            for ln in lines:
                ln.approved_amount = 0.0

        if approved_total <= 0.0:
            _zero(self.subscription_line_ids)
            _zero(self.infra_line_ids)
            _zero(self.model_line_ids)
            return

        remaining = approved_total

        if remaining + 1e-6 >= sub_total:
            for line in self.subscription_line_ids:
                line.approved_amount = sub_targets.get(line.id, 0.0)
            remaining -= sub_total
        else:
            ratio = (remaining / sub_total) if sub_total > 0.0 else 0.0
            for line in self.subscription_line_ids:
                line.approved_amount = sub_targets.get(line.id, 0.0) * ratio
            _zero(self.infra_line_ids)
            _zero(self.model_line_ids)
            return

        if remaining + 1e-6 >= infra_total:
            for line in self.infra_line_ids:
                line.approved_amount = infra_targets.get(line.id, 0.0)
            remaining -= infra_total
        else:
            ratio = (remaining / infra_total) if infra_total > 0.0 else 0.0
            for line in self.infra_line_ids:
                line.approved_amount = infra_targets.get(line.id, 0.0) * ratio
            _zero(self.model_line_ids)
            return

        if model_total > 0.0:
            if remaining + 1e-6 >= model_total:
                for line in self.model_line_ids:
                    line.approved_amount = model_targets.get(line.id, 0.0)
            else:
                ratio = remaining / model_total
                for line in self.model_line_ids:
                    line.approved_amount = (
                        model_targets.get(line.id, 0.0) * ratio
                    )
        else:
            _zero(self.model_line_ids)

    def action_auto_distribute_approved(self):
        for rec in self:
            rec._distribute_approved_amount()

    def _fixed_cost_floor(self):
        self.ensure_one()
        infra_req = sum(
            (line.requested_amount or 0.0) for line in self.infra_line_ids
        )
        sub_req = sum(
            (line.requested_amount or line.final_amount or 0.0)
            for line in self.subscription_line_ids
        )
        return infra_req + sub_req

    def _check_fixed_cost_floor(self):
        self.ensure_one()
        floor = self._fixed_cost_floor()
        if floor <= 0.0:
            return
        approved_total = self.approved_total or 0.0
        if approved_total + 1e-6 < floor:
            raise UserError(_(
                "Approved amount (USD %(approved).2f) must be at least the "
                "infrastructure + subscription cost (USD %(floor).2f). "
                "Either raise the approved amount to cover fixed costs or "
                "approve the full requested total."
            ) % {'approved': approved_total, 'floor': floor})

    def _check_can_submit(self):
        self.ensure_one()
        if not self.phase_id:
            raise UserError(_('Request has no phase.'))
        if not (
            self._user_has_role(RAISER_ROLE_XMLIDS)
            or self.env.user.has_group('base.group_system')
        ):
            raise UserError(_(
                'Only users with the PL, TPM or R&D role can submit budget '
                'requests for approval.'
            ))

    def _check_can_cto_review(self):
        self.ensure_one()
        if not (
            self._is_cto()
            or self.env.user.has_group('base.group_system')
        ):
            raise UserError(_(
                'Only users with the CTO role can review at this step.'
            ))

    def _check_can_cfo_approve(self):
        self.ensure_one()
        if not (
            self._is_cfo()
            or self.env.user.has_group('base.group_system')
        ):
            raise UserError(_(
                'Only users with the CFO role can approve or request '
                'changes at this step.'
            ))

    def _check_request_type_contents(self):
        self.ensure_one()
        rtype = self.request_type or 'budget'
        if rtype == 'budget':
            if not (
                self.model_line_ids
                or self.infra_line_ids
                or self.subscription_line_ids
            ):
                raise UserError(_(
                    'Budget request must include at least one model, '
                    'infrastructure or subscription line.'
                ))
        elif rtype == 'new_model':
            if not self.model_line_ids:
                raise UserError(_(
                    'New Model request must include at least one model line.'
                ))
            existing_models = set(
                self.budget_id.model_line_ids.mapped('ai_model_id').ids
            )
            new_models = [
                line.ai_model_id.id
                for line in self.model_line_ids
                if line.ai_model_id
                and line.ai_model_id.id not in existing_models
            ]
            if not new_models:
                raise UserError(_(
                    'New Model request must add at least one AI model that '
                    'is not already on the Project Budget.'
                ))
        elif rtype == 'topup':
            if (
                self.model_line_ids
                or self.infra_line_ids
                or self.subscription_line_ids
            ):
                raise UserError(_(
                    'Top-up request must be amount-only with no model, '
                    'infrastructure or subscription lines.'
                ))
        elif rtype == 'device':
            if not self.infra_line_ids:
                raise UserError(_(
                    'Device request must include at least one infrastructure '
                    'line.'
                ))

    def _approver_partner_ids(self):
        self.ensure_one()
        return self.budget_id.approver_user_ids.mapped('partner_id').ids

    def _cto_partner_ids(self):
        self.ensure_one()
        role_users = self._users_with_role(CTO_ROLE_XMLIDS)
        pool = self.budget_id.approver_user_ids
        overlap = pool & role_users if pool else role_users
        targets = overlap or role_users or pool
        return targets.mapped('partner_id').ids

    def _cfo_partner_ids(self):
        self.ensure_one()
        role_users = self._users_with_role(CFO_ROLE_XMLIDS)
        pool = self.budget_id.approver_user_ids
        overlap = pool & role_users if pool else role_users
        targets = overlap or role_users or pool
        return targets.mapped('partner_id').ids

    def _requester_partner_ids(self):
        self.ensure_one()
        return self.requester_id.partner_id.ids if self.requester_id else []

    def _send_thread_mail(self, template_xmlid, partner_ids, email_values=None):
        self.ensure_one()
        if self.env.context.get('ethara_skip_notify'):
            return
        project = self.ethara_project_id
        if not project:
            _logger.warning(
                'Request %s has no project; skipping %s.',
                self.name or self.id, template_xmlid,
            )
            return
        if not partner_ids:
            _logger.warning(
                'Request %s: skipping %s - no recipients resolved.',
                self.name or self.id, template_xmlid,
            )
            return
        dbname = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        project_id = project.id
        request_id = self.id
        template_ref = template_xmlid
        partners_snapshot = list(partner_ids)
        email_values_snapshot = dict(email_values) if email_values else None

        def _launch():
            def _run():
                try:
                    registry = Registry(dbname)
                    with registry.cursor() as new_cr:
                        new_env = api.Environment(new_cr, uid, ctx)
                        proj = new_env['ethara.project'].browse(project_id).exists()
                        req = new_env['ethara.project.phase.request'].browse(request_id).exists()
                        if proj and req:
                            proj._ethara_post_thread_message(
                                template_ref, req, partners_snapshot,
                                email_values=email_values_snapshot,
                            )
                        new_cr.commit()
                except Exception:
                    _logger.exception(
                        'Deferred phase-request mail failed for request %s',
                        request_id,
                    )
            threading.Thread(target=_run, daemon=True).start()

        self.env.cr.postcommit.add(_launch)

    def _post_project_thread(self, body):
        if self.env.context.get('ethara_skip_notify'):
            return
        dbname = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        posts = []
        for rec in self:
            project = rec.ethara_project_id
            if not project:
                continue
            partner_ids = project._ethara_thread_partner_ids()
            posts.append((project.id, rec.id, list(partner_ids or []), body))
        if not posts:
            return

        def _launch():
            def _run():
                try:
                    registry = Registry(dbname)
                    with registry.cursor() as new_cr:
                        new_env = api.Environment(new_cr, uid, ctx)
                        for project_id, rec_id, targets, body_str in posts:
                            proj = new_env['ethara.project'].sudo().browse(project_id).exists()
                            if not proj:
                                continue
                            try:
                                proj.message_post(
                                    body=body_str,
                                    subtype_xmlid='mail.mt_note',
                                    message_type='notification',
                                    partner_ids=targets,
                                )
                            except Exception:
                                _logger.exception(
                                    'Deferred phase-request chatter post failed for %s',
                                    rec_id,
                                )
                        new_cr.commit()
                except Exception:
                    _logger.exception('Deferred phase-request chatter setup failed')
            threading.Thread(target=_run, daemon=True).start()

        self.env.cr.postcommit.add(_launch)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('ethara.project.phase.request')
                    or 'New'
                )
        records = super().create(vals_list)
        for rec in records:
            body = _(
                '<p><strong>Phase Budget Request created:</strong> %s '
                '(Phase: %s, Requested: %.2f USD)</p>'
            ) % (
                rec.name or '',
                rec.phase_id.name or '',
                rec.requested_total or 0.0,
            )
            rec._post_project_thread(body)
        return records

    def write(self, vals):
        if 'request_type' in vals:
            for rec in self:
                if (
                    rec.state not in ('draft', 'changes_required')
                    and vals['request_type'] != rec.request_type
                ):
                    raise UserError(_(
                        'Request Type cannot be changed once the request '
                        'has been submitted.'
                    ))
        return super().write(vals)

    def action_submit_for_approval(self):
        for rec in self:
            if rec.state not in ('draft', 'changes_required'):
                raise UserError(_(
                    'Only Draft or Changes-Required requests can be '
                    'submitted for approval.'
                ))
            rec._check_can_submit()
            rec._check_request_type_contents()
            if (rec.requested_total or 0.0) <= 0.0:
                raise UserError(_(
                    'Requested total must be greater than zero.'
                ))
            if not rec.budget_id.approver_user_ids:
                raise UserError(_(
                    'Project Budget has no approvers configured.'
                ))
            for line in rec.model_line_ids:
                if not line.approved_amount:
                    line.approved_amount = line.requested_amount
            for line in rec.infra_line_ids:
                if not line.approved_amount:
                    line.approved_amount = line.requested_amount
                if not line.start_date:
                    line.start_date = rec.phase_id.start_date
                if not line.end_date:
                    line.end_date = rec.phase_id.end_date
            for line in rec.subscription_line_ids:
                if not line.approved_amount:
                    line.approved_amount = (
                        line.requested_amount or line.final_amount
                    )
            if not rec.approved_total:
                rec.approved_total = rec.requested_total
            rec.write({
                'state': 'cto_review',
                'revision_no': (rec.revision_no or 0) + 1,
            })
            email_values = {}
            if rec.subject:
                email_values['subject'] = rec.subject
            if rec.attachment_ids:
                email_values['attachment_ids'] = [(6, 0, rec.attachment_ids.ids)]
            submitted_partner_ids = list(set(
                rec._approver_partner_ids()
                + rec._requester_partner_ids()
            ))
            rec._send_thread_mail(
                TEMPLATE_SUBMITTED,
                submitted_partner_ids,
                email_values=email_values,
            )
            rec._send_thread_mail(
                TEMPLATE_CTO_REVIEW,
                rec._cto_partner_ids(),
                email_values=email_values,
            )

    def action_cto_approve(self):
        for rec in self:
            if rec.state != 'cto_review':
                raise UserError(_(
                    'Only requests in CTO Review can be approved by the CTO.'
                ))
            rec._check_can_cto_review()
            rec._distribute_approved_amount()
            if (rec.approved_total or 0.0) <= 0.0:
                raise UserError(_(
                    "Approved total must be greater than zero. Use "
                    "'Send Back for Changes' if the request is not acceptable."
                ))
            rec.write({
                'state': 'cfo_review',
                'cto_reviewer_id': self.env.user.id,
                'cto_review_date': fields.Datetime.now(),
            })
            rec._send_thread_mail(
                TEMPLATE_CFO_REVIEW,
                list(set(rec._cfo_partner_ids() + rec._requester_partner_ids())),
            )

    def action_cto_reject(self):
        self.ensure_one()
        if self.state != 'cto_review':
            raise UserError(_(
                'Only requests in CTO Review can be sent back by the CTO.'
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('CTO: Send Back for Changes'),
            'res_model': 'ethara.project.phase.request.review.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_request_id': self.id,
                'default_mode': 'cto_reject',
            },
        }

    def action_cfo_approve(self):
        for rec in self:
            if rec.state != 'cfo_review':
                raise UserError(_(
                    'Only requests in CFO Approval can be approved by the CFO.'
                ))
            rec._check_can_cfo_approve()
            if (rec.approved_total or 0.0) <= 0.0:
                raise UserError(_(
                    "Approved total must be greater than zero. Use "
                    "'Request Changes' if the request is not acceptable."
                ))
            rec._check_fixed_cost_floor()
            rec._distribute_approved_amount()
            is_partial = (
                (rec.approved_total or 0.0) < (rec.requested_total or 0.0)
                or any(
                    (line.approved_amount or 0.0) < (line.requested_amount or 0.0)
                    for line in rec.model_line_ids
                )
                or any(
                    (line.approved_amount or 0.0) < (line.requested_amount or 0.0)
                    for line in rec.infra_line_ids
                )
            )
            new_state = 'partially_approved' if is_partial else 'approved'
            now = fields.Datetime.now()
            rec.write({
                'state': new_state,
                'cfo_approver_id': self.env.user.id,
                'cfo_approval_date': now,
                'approver_id': self.env.user.id,
                'approval_date': now,
            })
            rec._propagate_to_phase_and_budget()
            cto_partner_ids = (
                rec.cto_reviewer_id.partner_id.ids
                if rec.cto_reviewer_id else []
            )
            rec._send_thread_mail(
                TEMPLATE_APPROVED,
                list(set(rec._requester_partner_ids() + cto_partner_ids)),
            )

    def action_cfo_request_changes(self):
        self.ensure_one()
        if self.state != 'cfo_review':
            raise UserError(_(
                'Only requests in CFO Approval can be sent back by the CFO.'
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('CFO: Request Changes'),
            'res_model': 'ethara.project.phase.request.review.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_request_id': self.id,
                'default_mode': 'cfo_request_changes',
            },
        }

    def _do_cto_reject(self, note):
        self.ensure_one()
        if self.state != 'cto_review':
            raise UserError(_(
                'Only requests in CTO Review can be sent back by the CTO.'
            ))
        self._check_can_cto_review()
        self.write({
            'state': 'changes_required',
            'cto_reviewer_id': self.env.user.id,
            'cto_review_date': fields.Datetime.now(),
            'cto_review_note': note,
            'rejection_reason': note,
            'rejected_by': self.env.user.id,
        })
        self._send_thread_mail(
            TEMPLATE_CTO_REJECTED,
            self._requester_partner_ids(),
        )

    def _do_cfo_request_changes(self, note):
        self.ensure_one()
        if self.state != 'cfo_review':
            raise UserError(_(
                'Only requests in CFO Approval can be sent back by the CFO.'
            ))
        self._check_can_cfo_approve()
        now = fields.Datetime.now()
        self.write({
            'state': 'changes_required',
            'cfo_approver_id': self.env.user.id,
            'cfo_approval_date': now,
            'cfo_change_request_note': note,
            'approver_id': self.env.user.id,
            'approval_date': now,
            'rejection_reason': note,
            'rejected_by': self.env.user.id,
        })
        cto_partner_ids = (
            self.cto_reviewer_id.partner_id.ids
            if self.cto_reviewer_id else []
        )
        self._send_thread_mail(
            TEMPLATE_CFO_CHANGES,
            list(set(self._requester_partner_ids() + cto_partner_ids)),
        )

    def action_withdraw(self):
        for rec in self:
            if rec.state not in (
                'draft', 'cto_review', 'cfo_review', 'changes_required',
            ):
                raise UserError(_(
                    'Only Draft, CTO Review, CFO Approval or '
                    'Changes-Required requests can be withdrawn.'
                ))
            if rec.requester_id and self.env.user != rec.requester_id:
                raise UserError(_(
                    'Only the requester can withdraw this request.'
                ))
            rec.state = 'withdrawn'
            rec._post_project_thread(_(
                '<p><strong>Phase Budget Request withdrawn:</strong> %s</p>'
            ) % (rec.name or ''))

    def _propagate_to_phase_and_budget(self):
        self.ensure_one()
        phase = self.phase_id
        budget = phase.budget_id
        PhaseModelLine = self.env['ethara.project.phase.model.line']
        PhaseInfraLine = self.env['ethara.project.phase.infra.line']
        PhaseSubLine = self.env['ethara.project.phase.subscription.line']
        BudgetModelLine = self.env['ethara.project.budget.model.line']
        BudgetInfraLine = self.env['ethara.project.budget.infra.line']
        BudgetSubLine = self.env['ethara.project.budget.subscription.line']

        phase_model_by_key = {
            line.ai_model_id.id: line
            for line in phase.model_line_ids
            if line.ai_model_id
        }
        budget_model_keys = {
            line.ai_model_id.id
            for line in budget.model_line_ids
            if line.ai_model_id
        }
        for line in self.model_line_ids:
            if (line.approved_amount or 0.0) <= 0.0 or not line.ai_model_id:
                continue
            existing = phase_model_by_key.get(line.ai_model_id.id)
            if existing:
                existing.approved_amount = (
                    (existing.approved_amount or 0.0)
                    + (line.approved_amount or 0.0)
                )
            else:
                new_line = PhaseModelLine.create({
                    'phase_id': phase.id,
                    'ai_model_id': line.ai_model_id.id,
                    'ai_model_name': line.ai_model_name or False,
                    'cost_type': line.cost_type or 'per_task',
                    'per_trajectory_cost': line.per_trajectory_cost or 0.0,
                    'iterations': line.iterations or 0,
                    'per_task_cost': line.per_task_cost or 0.0,
                    'approved_amount': line.approved_amount or 0.0,
                })
                phase_model_by_key[line.ai_model_id.id] = new_line
            if line.ai_model_id.id not in budget_model_keys:
                BudgetModelLine.create({
                    'budget_id': budget.id,
                    'ai_model_id': line.ai_model_id.id,
                    'ai_model_name': line.ai_model_name or False,
                    'cost_type': line.cost_type or 'per_task',
                    'per_trajectory_cost': line.per_trajectory_cost or 0.0,
                    'iterations': line.iterations or 0,
                    'per_task_cost': line.per_task_cost or 0.0,
                })
                budget_model_keys.add(line.ai_model_id.id)

        phase_infra_by_key = {
            line.infra_type_id.id: line
            for line in phase.infra_line_ids
            if line.infra_type_id
        }
        budget_infra_keys = {
            line.infra_type_id.id
            for line in budget.infra_line_ids
            if line.infra_type_id
        }
        for line in self.infra_line_ids:
            if (line.approved_amount or 0.0) <= 0.0 or not line.infra_type_id:
                continue
            existing = phase_infra_by_key.get(line.infra_type_id.id)
            if existing:
                amount = line.approved_amount or 0.0
                existing.approved_amount = (existing.approved_amount or 0.0) + amount
                existing.budget_amount = (existing.budget_amount or 0.0) + amount
                if line.start_date and (
                    not existing.start_date
                    or line.start_date < existing.start_date
                ):
                    existing.start_date = line.start_date
                if line.end_date and (
                    not existing.end_date
                    or line.end_date > existing.end_date
                ):
                    existing.end_date = line.end_date
            else:
                new_line = PhaseInfraLine.create({
                    'phase_id': phase.id,
                    'infra_type_id': line.infra_type_id.id,
                    'description': line.description or False,
                    'budget_amount': line.approved_amount or 0.0,
                    'approved_amount': line.approved_amount or 0.0,
                    'start_date': line.start_date or False,
                    'end_date': line.end_date or False,
                    'instance_type': line.instance_type or False,
                    'unit_price_usd': line.unit_price_usd or 0.0,
                    'price_unit': line.price_unit or False,
                    'quantity': line.quantity or 1.0,
                    'duration_hours': line.duration_hours or 730.0,
                    'ebs_storage_gb': line.ebs_storage_gb or 0.0,
                    'volume_type': line.volume_type or 'gp3',
                    'volume_rate_usd_per_gb_mo': line.volume_rate_usd_per_gb_mo or 0.0,
                })
                phase_infra_by_key[line.infra_type_id.id] = new_line
            if line.infra_type_id.id not in budget_infra_keys:
                BudgetInfraLine.create({
                    'budget_id': budget.id,
                    'infra_type_id': line.infra_type_id.id,
                    'description': line.description or False,
                    'budget_amount': line.approved_amount or 0.0,
                    'start_date': line.start_date or False,
                    'end_date': line.end_date or False,
                    'instance_type': line.instance_type or False,
                    'unit_price_usd': line.unit_price_usd or 0.0,
                    'price_unit': line.price_unit or False,
                    'quantity': line.quantity or 1.0,
                    'duration_hours': line.duration_hours or 730.0,
                    'ebs_storage_gb': line.ebs_storage_gb or 0.0,
                    'volume_type': line.volume_type or 'gp3',
                    'volume_rate_usd_per_gb_mo': line.volume_rate_usd_per_gb_mo or 0.0,
                })
                budget_infra_keys.add(line.infra_type_id.id)

        phase_sub_by_key = {
            line.subscription_id.id: line
            for line in phase.subscription_line_ids
            if line.subscription_id
        }
        budget_sub_by_key = {
            line.subscription_id.id: line
            for line in budget.subscription_line_ids
            if line.subscription_id
        }
        sub_start = fields.Date.to_date(
            self.approval_date or fields.Datetime.now()
        )
        sub_end = sub_start + timedelta(days=30)
        for line in self.subscription_line_ids:
            if (line.approved_amount or 0.0) <= 0.0 or not line.subscription_id:
                continue
            existing = phase_sub_by_key.get(line.subscription_id.id)
            if existing:
                existing.approved_amount = (
                    (existing.approved_amount or 0.0)
                    + (line.approved_amount or 0.0)
                )
                merged_users = existing.assigned_user_ids | line.assigned_user_ids
                existing.assigned_user_ids = [(6, 0, merged_users.ids)]
                existing.start_date = min(existing.start_date or sub_start, sub_start)
                existing.end_date = max(existing.end_date or sub_end, sub_end)
            else:
                phase_sub_by_key[line.subscription_id.id] = PhaseSubLine.create({
                    'phase_id': phase.id,
                    'subscription_id': line.subscription_id.id,
                    'assigned_user_ids': [(6, 0, line.assigned_user_ids.ids)],
                    'approved_amount': line.approved_amount or 0.0,
                    'start_date': sub_start,
                    'end_date': sub_end,
                })

            proj_existing = budget_sub_by_key.get(line.subscription_id.id)
            if proj_existing:
                proj_existing.start_date = min(
                    proj_existing.start_date or sub_start, sub_start
                )
                proj_existing.end_date = max(
                    proj_existing.end_date or sub_end, sub_end
                )
            else:
                budget_sub_by_key[line.subscription_id.id] = BudgetSubLine.create({
                    'budget_id': budget.id,
                    'subscription_id': line.subscription_id.id,
                    'assigned_user_ids': [(6, 0, line.assigned_user_ids.ids)],
                    'approved_amount': line.approved_amount or 0.0,
                    'start_date': sub_start,
                    'end_date': sub_end,
                })

        if phase.state in ('draft', 'rejected', 'withdrawn'):
            phase.write({'state': 'approved'})
