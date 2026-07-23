import logging
import threading
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


PHASE_STATE_SELECTION = [
    ('draft', 'Draft'),
    ('approved', 'Approved'),
    ('in_progress', 'In Progress'),
    ('delivered', 'Delivered'),
    ('closed', 'Closed'),
    ('rejected', 'Rejected'),
    ('withdrawn', 'Withdrawn'),
]

HEALTH_SELECTION = [
    ('unknown', 'Unknown'),
    ('healthy', 'Healthy'),
    ('warning', 'Warning'),
    ('at_risk', 'At Risk'),
    ('critical', 'Critical'),
]

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0
ALERT_80_THRESHOLD = 80.0
ALERT_100_THRESHOLD = 100.0


class EtharaProjectPhase(models.Model):
    _name = 'ethara.project.phase'
    _description = 'Ethara Project Phase Budget'
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
        ondelete='restrict',
        tracking=True,
    )
    ethara_project_id = fields.Many2one(
        related='budget_id.ethara_project_id',
        store=True,
        readonly=True,
        index=True,
    )
    model_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.model.line',
        inverse_name='phase_id',
        string='Model Lines',
        copy=True,
    )
    infra_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.infra.line',
        inverse_name='phase_id',
        string='Infrastructure Lines',
        copy=True,
    )
    subscription_line_ids = fields.One2many(
        comodel_name='ethara.project.phase.subscription.line',
        inverse_name='phase_id',
        string='Subscription Lines',
        copy=True,
    )
    request_ids = fields.One2many(
        comodel_name='ethara.project.phase.request',
        inverse_name='phase_id',
        string='Budget Requests',
    )
    request_count = fields.Integer(
        string='Request Count',
        compute='_compute_request_count',
    )
    description = fields.Text(string='Description')
    completion_description = fields.Text(string='Completion Description')
    connected_model = fields.Char(string='Source Model')
    total_tasks = fields.Integer(string='Total Tasks', tracking=True)
    est_trajectories_per_task = fields.Integer(
        string='Est. Trajectories / Task',
        default=0,
        tracking=True,
        copy=False,
        help='Snapshot copied from the parent budget at phase creation. '
             'Used to compute submitted trajectories at delivery time.',
    )
    submitted_task_count = fields.Integer(
        string='Submitted Task Count',
        default=0,
        tracking=True,
        copy=False,
        help='Actual task count submitted by R&D at phase delivery.',
    )
    delivered_per_task_cost = fields.Float(
        string='Delivered $/Task',
        default=0.0,
        tracking=True,
        copy=False,
        help='Actual per-task cost declared by R&D at phase delivery.',
    )
    submitted_trajectories = fields.Integer(
        string='Submitted Trajectories',
        default=0,
        tracking=True,
        copy=False,
        help='Trajectories declared by R&D at phase delivery. '
             'Free integer input, independent of the budget-side estimate.',
    )
    models_used = fields.Char(
        string='Models Used',
        default='',
        tracking=True,
        copy=False,
        help='Free-text list of AI models used, entered by R&D at delivery '
             "(e.g. 'Opus 4.8, Sonnet 4.6').",
    )
    submitted_batch_total = fields.Float(
        string='Submitted Batch Total (USD)',
        compute='_compute_submitted_totals',
        store=True,
    )
    daily_task_ids = fields.One2many(
        'ethara.project.phase.daily.task',
        'phase_id',
        string='Daily Tasks',
    )
    connected_record_ids = fields.One2many(
        'ethara.project.phase.connected.record',
        'phase_id',
        string='Connected Records',
    )
    feedback_ids = fields.One2many(
        'ethara.project.phase.feedback',
        'phase_id',
        string='Feedback',
    )
    info_link_ids = fields.One2many(
        'ethara.project.phase.info.link',
        'phase_id',
        string='Info Links',
    )
    done_tasks = fields.Integer(
        string='Done Tasks',
        compute='_compute_task_progress',
        store=True,
    )
    remaining_tasks = fields.Integer(
        string='Remaining Tasks',
        compute='_compute_task_progress',
        store=True,
    )
    estimated_cost = fields.Float(
        string='Estimated Cost (USD)',
        compute='_compute_estimated_cost',
        store=True,
    )
    buffer_pct = fields.Float(
        string='Buffer %',
        default=0.0,
        tracking=True,
    )
    phase_budget = fields.Float(
        string='Phase Budget (USD)',
        compute='_compute_phase_budget',
        store=True,
    )
    approved_amount = fields.Float(
        string='Approved Amount (USD)',
        compute='_compute_approved_amount',
        store=True,
        tracking=True,
    )
    start_date = fields.Date(string='Start Date', tracking=True)
    end_date = fields.Date(string='End Date', tracking=True)
    state = fields.Selection(
        selection=PHASE_STATE_SELECTION,
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
    delivered_date = fields.Datetime(readonly=True, tracking=True)
    closed_remaining = fields.Float(
        string='Returned to Budget (USD)',
        readonly=True,
        tracking=True,
    )
    carried_over_amount = fields.Float(
        string='Carried Over (USD)',
        default=0.0,
        readonly=True,
        tracking=True,
        copy=False,
    )
    consumed_cost = fields.Float(
        string='Consumed (USD)',
        compute='_compute_consumed_cost',
    )
    consumed_pct = fields.Float(
        string='Consumed %',
        compute='_compute_consumed_cost',
    )
    remaining_cost = fields.Float(
        string='Remaining (USD)',
        compute='_compute_consumed_cost',
    )
    health_status = fields.Selection(
        selection=HEALTH_SELECTION,
        string='Budget Health',
        compute='_compute_health_status',
        store=True,
    )
    alert_80_sent = fields.Boolean(readonly=True, copy=False)
    alert_100_sent = fields.Boolean(readonly=True, copy=False)
    active = fields.Boolean(default=True)

    @api.depends('request_ids')
    def _compute_request_count(self):
        for rec in self:
            rec.request_count = len(rec.request_ids)

    @api.depends('daily_task_ids.done_count', 'total_tasks')
    def _compute_task_progress(self):
        for rec in self:
            done = sum(rec.daily_task_ids.mapped('done_count'))
            rec.done_tasks = done
            rec.remaining_tasks = max((rec.total_tasks or 0) - done, 0)

    @api.depends('submitted_task_count', 'delivered_per_task_cost')
    def _compute_submitted_totals(self):
        for rec in self:
            submitted = rec.submitted_task_count or 0
            rec.submitted_batch_total = submitted * (rec.delivered_per_task_cost or 0.0)

    @api.depends(
        'total_tasks',
        'model_line_ids.per_task_cost',
        'infra_line_ids.per_day_cost',
        'subscription_line_ids.final_amount',
        'start_date',
        'end_date',
    )
    def _compute_estimated_cost(self):
        for rec in self:
            per_task = sum(rec.model_line_ids.mapped('per_task_cost'))
            infra_per_day = sum(rec.infra_line_ids.mapped('per_day_cost'))
            sub_total = sum(rec.subscription_line_ids.mapped('final_amount'))
            duration_days = 0
            if rec.start_date and rec.end_date and rec.end_date >= rec.start_date:
                duration_days = (rec.end_date - rec.start_date).days + 1
            rec.estimated_cost = (
                (rec.total_tasks or 0) * per_task
                + duration_days * infra_per_day
                + sub_total
            )

    @api.depends('estimated_cost', 'buffer_pct')
    def _compute_phase_budget(self):
        for rec in self:
            buffer = (rec.buffer_pct or 0.0) / 100.0
            rec.phase_budget = (rec.estimated_cost or 0.0) * (1.0 + buffer)

    @api.depends(
        'request_ids.state',
        'request_ids.approved_total',
        'carried_over_amount',
    )
    def _compute_approved_amount(self):
        for rec in self:
            request_total = sum(
                req.approved_total
                for req in rec.request_ids
                if req.state in ('approved', 'partially_approved')
            )
            rec.approved_amount = (rec.carried_over_amount or 0.0) + request_total

    def _cost_line_domain(self):
        self.ensure_one()
        if not (self.ethara_project_id and self.start_date and self.end_date):
            return None
        return [
            ('ethara_project_id', '=', self.ethara_project_id.id),
            ('granularity', '=', 'day'),
            ('period', '>=', self.start_date),
            ('period', '<=', self.end_date),
        ]

    @api.depends(
        'start_date',
        'end_date',
        'ethara_project_id',
        'approved_amount',
        'subscription_line_ids.approved_amount',
    )
    def _compute_consumed_cost(self):
        Line = self.env['ethara.project.cost.line'].sudo()
        for rec in self:
            domain = rec._cost_line_domain()
            fetched = 0.0
            if domain:
                fetched = sum(
                    Line.search(domain + [('is_model_breakdown', '=', False)])
                    .mapped('amount_source')
                )
            sub_consumed = sum(
                sub.approved_amount for sub in rec.subscription_line_ids
            )
            consumed = fetched + sub_consumed
            rec.consumed_cost = consumed
            rec.remaining_cost = (rec.approved_amount or 0.0) - consumed
            rec.consumed_pct = (
                (consumed / rec.approved_amount) * 100.0
                if rec.approved_amount else 0.0
            )

    @api.depends('approved_amount', 'consumed_pct', 'state')
    def _compute_health_status(self):
        for rec in self:
            if rec.state == 'draft' or not rec.approved_amount:
                rec.health_status = 'unknown'
                continue
            pct = rec.consumed_pct or 0.0
            if pct < HEALTH_HEALTHY_PCT:
                rec.health_status = 'healthy'
            elif pct < HEALTH_WARNING_PCT:
                rec.health_status = 'warning'
            elif pct < HEALTH_AT_RISK_PCT:
                rec.health_status = 'at_risk'
            else:
                rec.health_status = 'critical'

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
                                kwargs = {
                                    'body': body_str,
                                    'subtype_xmlid': 'mail.mt_note',
                                    'message_type': 'notification',
                                    'partner_ids': targets,
                                }
                                kwargs = proj._ethara_thread_post_kwargs(kwargs)
                                message = proj.message_post(**kwargs)
                                proj._ethara_capture_root(message)
                            except Exception:
                                _logger.exception(
                                    'Deferred phase chatter post failed for phase %s',
                                    rec_id,
                                )
                        new_cr.commit()
                except Exception:
                    _logger.exception('Deferred phase chatter setup failed')
            threading.Thread(target=_run, daemon=True).start()

        self.env.cr.postcommit.add(_launch)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('ethara.project.phase')
                    or 'New'
                )
            if 'est_trajectories_per_task' not in vals and vals.get('budget_id'):
                budget = self.env['ethara.project.budget'].browse(vals['budget_id'])
                vals['est_trajectories_per_task'] = budget.est_trajectories_per_task or 0
        records = super().create(vals_list)
        for rec in records:
            if rec.budget_id and not rec.model_line_ids:
                rec.model_line_ids = [
                    (0, 0, {
                        'ai_model_id': line.ai_model_id.id,
                        'ai_model_name': line.ai_model_name or False,
                        'cost_type': line.cost_type or 'per_task',
                        'per_trajectory_cost': line.per_trajectory_cost or 0.0,
                        'iterations': line.iterations or 0,
                        'per_task_cost': line.per_task_cost,
                    })
                    for line in rec.budget_id.model_line_ids
                ]
            if rec.budget_id and not rec.carried_over_amount:
                pool = rec.budget_id.batch_budget_remain or 0.0
                if pool > 0.0:
                    rec.carried_over_amount = pool
                    rec.budget_id.batch_budget_remain = 0.0
            body = _(
                '<p><strong>Phase created:</strong> %s '
                '(Project Budget: %s, Estimated: %.2f USD)</p>'
            ) % (
                rec.name or '',
                rec.budget_id.name or '',
                rec.estimated_cost or 0.0,
            )
            rec._post_project_thread(body)
        return records

    def action_refresh_model_lines(self):
        for rec in self:
            if not rec.budget_id:
                raise UserError(_('Pick a Project Budget first.'))
            if rec.state != 'draft':
                raise UserError(_(
                    'Model lines can only be refreshed in Draft state.'
                ))
            rec.model_line_ids = [(5, 0, 0)] + [
                (0, 0, {
                    'ai_model_id': line.ai_model_id.id,
                    'ai_model_name': line.ai_model_name or False,
                    'cost_type': line.cost_type or 'per_task',
                    'per_trajectory_cost': line.per_trajectory_cost or 0.0,
                    'iterations': line.iterations or 0,
                    'per_task_cost': line.per_task_cost,
                })
                for line in rec.budget_id.model_line_ids
            ]

    def action_withdraw(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('Only Draft phases can be withdrawn.'))
            if rec.requester_id and self.env.user != rec.requester_id:
                raise UserError(_(
                    'Only the requester can withdraw this phase.'
                ))
            rec.state = 'withdrawn'
            rec._post_project_thread(_(
                '<p><strong>Phase withdrawn:</strong> %s</p>'
            ) % (rec.name or ''))

    def action_reset_to_draft(self):
        for rec in self:
            if rec.state not in ('rejected', 'withdrawn'):
                raise UserError(_(
                    'Only Rejected or Withdrawn phases can be reset.'
                ))
            rec.write({
                'state': 'draft',
                'approver_id': False,
                'approval_date': False,
                'rejection_reason': False,
            })

    def _find_next_phase(self):
        self.ensure_one()
        if not self.budget_id:
            return self.env['ethara.project.phase']
        domain = [
            ('budget_id', '=', self.budget_id.id),
            ('id', '!=', self.id),
            ('state', 'not in', ('delivered', 'closed', 'rejected', 'withdrawn')),
        ]
        if self.start_date:
            domain.append(('start_date', '>=', self.start_date))
        return self.env['ethara.project.phase'].search(
            domain, order='start_date asc, id asc', limit=1,
        )

    def action_deliver(self):
        for rec in self:
            if rec.state not in ('approved', 'in_progress'):
                raise UserError(_(
                    'Only Approved or In Progress phases can be delivered.'
                ))
            remaining = (rec.approved_amount or 0.0) - (rec.consumed_cost or 0.0)
            if remaining < 0.0:
                remaining = 0.0
            rec.write({
                'state': 'delivered',
                'delivered_date': fields.Datetime.now(),
                'closed_remaining': remaining,
            })
            if remaining > 0.0 and rec.budget_id:
                next_phase = rec._find_next_phase()
                if next_phase:
                    next_phase.carried_over_amount = (
                        next_phase.carried_over_amount or 0.0
                    ) + remaining
                else:
                    rec.budget_id.batch_budget_remain = (
                        rec.budget_id.batch_budget_remain or 0.0
                    ) + remaining
            if not self.env.context.get('ethara_skip_notify'):
                project = rec.ethara_project_id
                if project:
                    partner_ids = project._ethara_thread_partner_ids()
                    if partner_ids:
                        project.sudo()._ethara_post_thread_message(
                            'ethara_project.mail_template_ethara_phase_delivered',
                            rec,
                            partner_ids,
                        )

    def action_close(self):
        for rec in self:
            if rec.state != 'delivered':
                raise UserError(_(
                    'Only Delivered phases can be closed.'
                ))
            rec.state = 'closed'
            rec._post_project_thread(_(
                '<p><strong>Phase closed:</strong> %s</p>'
            ) % (rec.name or ''))

    def action_restart(self):
        for rec in self:
            if rec.state != 'delivered':
                raise UserError(_(
                    'Only Delivered phases can be restarted.'
                ))
            budget = rec.budget_id
            pool = (budget.batch_budget_remain or 0.0) if budget else 0.0
            if pool < 0.0:
                pool = 0.0
            rec.write({
                'state': 'in_progress',
                'delivered_date': False,
                'closed_remaining': 0.0,
                'carried_over_amount': (rec.carried_over_amount or 0.0) + pool,
            })
            if budget and pool > 0.0:
                budget.batch_budget_remain = 0.0

    def _threshold_partner_ids(self):
        self.ensure_one()
        project = self.ethara_project_id
        if not project:
            return []
        return project._ethara_thread_partner_ids()

    def _send_threshold_mail(self, template_xmlid):
        self.ensure_one()
        project = self.ethara_project_id
        if not project:
            return
        partner_ids = self._threshold_partner_ids()
        project._ethara_post_thread_message(template_xmlid, self, partner_ids)

    def _check_threshold_alerts(self):
        for rec in self:
            if rec.state not in ('approved', 'in_progress'):
                continue
            if not rec.approved_amount:
                continue
            pct = rec.consumed_pct or 0.0
            if pct >= ALERT_100_THRESHOLD and not rec.alert_100_sent:
                rec._send_threshold_mail(
                    'ethara_project.mail_template_ethara_phase_threshold_100'
                )
                rec.alert_100_sent = True
                rec.alert_80_sent = True
            elif pct >= ALERT_80_THRESHOLD and not rec.alert_80_sent:
                rec._send_threshold_mail(
                    'ethara_project.mail_template_ethara_phase_threshold_80'
                )
                rec.alert_80_sent = True

    def action_view_requests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Phase Budget Requests'),
            'res_model': 'ethara.project.phase.request',
            'view_mode': 'list,form',
            'domain': [('phase_id', '=', self.id)],
            'context': {'default_phase_id': self.id},
        }

    def action_open_daily_task_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Log Daily Task'),
            'res_model': 'ethara.project.phase.daily.task.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_phase_id': self.id},
        }

    def action_open_request_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Phase Budget Request'),
            'res_model': 'ethara.project.phase.request.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_phase_id': self.id},
        }

    def action_view_daily_tasks(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Daily Tasks'),
            'res_model': 'ethara.project.phase.daily.task',
            'view_mode': 'list,form',
            'domain': [('phase_id', '=', self.id)],
            'context': {'default_phase_id': self.id},
        }
