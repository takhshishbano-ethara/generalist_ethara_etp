# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class KenseiTrackerAllocation(models.Model):
    """A task assigned to a tasker, tracked through the RL pipeline.

    The workflow status bar drives which stage inputs are shown/required on the
    form. Inherits mail.thread so every tracked change is logged with who + when.
    """
    _name = 'kensei.tracker.allocation'
    _description = 'Kensei Tracker Task Allocation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'assigned_date desc, id desc'
    _rec_name = 'task_id'

    # ------------------------------------------------------------------ #
    #  Selections
    # ------------------------------------------------------------------ #
    STATUS_SELECTION = [
        ('task_created', 'Task Created'),
        ('in_progress', 'In Progress (Authoring)'),
        ('tasker_qc_completed', 'Tasker QC Completed'),
        ('pl_verified', 'PL Verified'),
        ('ready_baseline', 'Ready for Baseline Trajectory'),
        ('baseline_generated', 'Baseline Trajectory Generated'),
        ('pass1', 'Pass @1 Generation'),
        ('manual_qc', 'Manual QC'),
        ('deliverable', 'Deliverable'),
        ('failed', 'Failed'),
    ]
    STAGE_STATUS_SELECTION = [
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]

    # ------------------------------------------------------------------ #
    #  Basic information
    # ------------------------------------------------------------------ #
    tasker_name = fields.Char(string='Tasker Name', tracking=True)
    tasker_email = fields.Char(string='Tasker Email', required=True, index=True, tracking=True)
    tasker_user_id = fields.Many2one(
        'res.users', string='Tasker User', compute='_compute_tasker_user',
        store=True, index=True,
        help='Resolved from the Tasker Email; used to scope what a tasker sees.',
    )
    task_id = fields.Char(string='Task ID', required=True, index=True, tracking=True)
    tasker_id = fields.Char(string='Tasker ID', index=True, tracking=True)
    pl_id = fields.Many2one(
        'hr.employee', string='PL Assigned', index=True, tracking=True,
        help='Project Lead responsible for this task.',
    )
    team_lead_id = fields.Many2one(
        'hr.employee', string='Team Lead / Manager', index=True, tracking=True,
    )
    project = fields.Char(string='Project', index=True, tracking=True)
    assigned_date = fields.Date(
        string='Assignment Date', default=fields.Date.context_today, index=True, tracking=True,
    )
    persona_id = fields.Many2one('kensei.persona', string='Persona', tracking=True)
    function = fields.Selection(
        selection=[
            ('task_authoring', 'Task Authoring'),
            ('trajectory', 'Trajectory'),
            ('manual_qc', 'Manual QC'),
        ],
        string='Function', default='task_authoring', index=True, tracking=True,
    )
    # Auto-derived from the fields completed at each stage (see _compute_status).
    status = fields.Selection(
        selection=STATUS_SELECTION, string='Current Status',
        compute='_compute_status', store=True, readonly=True,
        index=True, tracking=True,
    )
    notes = fields.Text(string='Notes')

    # ------------------------------------------------------------------ #
    #  Evaluation metrics (%) — captured at "Baseline Generated → Done"
    # ------------------------------------------------------------------ #
    rubric_score = fields.Float(string='Rubric Score (%)', tracking=True)
    pytest_score = fields.Float(string='Pytest Score (%)', tracking=True)
    overall_score = fields.Float(string='Overall Score (%)', tracking=True)
    qced_by = fields.Many2one('res.users', string='QCed By', tracking=True)

    # ------------------------------------------------------------------ #
    #  Stage-specific inputs (shown dynamically on the form by status)
    # ------------------------------------------------------------------ #
    drive_link = fields.Char(string='Drive Link', tracking=True)
    pl_verified_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='PL Verification',
        default='in_progress', tracking=True,
    )
    pl_verified_reason = fields.Text(string='PL Verification Reason')
    baseline_drive_link = fields.Char(string='Baseline Trajectory Drive Link', tracking=True)
    baseline_gen_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Baseline Generation',
        default='in_progress', tracking=True,
    )
    baseline_gen_reason = fields.Text(string='Baseline Generation Reason')
    manual_qc_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Manual QC Result',
        default='in_progress', tracking=True,
    )
    manual_qc_reason = fields.Text(string='Manual QC Reason')
    failure_reason = fields.Text(
        string='Failure Reason', compute='_compute_failure_reason', store=True,
    )

    # ------------------------------------------------------------------ #
    #  Final status + completion timestamp (completion date for Daily Tracker)
    # ------------------------------------------------------------------ #
    final_status = fields.Selection(
        selection=[
            ('in_progress', 'In Progress'),
            ('deliverable', 'Deliverable'),
            ('failed', 'Failed'),
        ],
        string='Final Status', compute='_compute_final_status', store=True,
        readonly=True, index=True, tracking=True,
    )
    date_final = fields.Datetime(string='Completed / Terminated On', readonly=True)

    # ------------------------------------------------------------------ #
    #  Computes
    # ------------------------------------------------------------------ #
    @api.depends('tasker_email')
    def _compute_tasker_user(self):
        Users = self.env['res.users'].sudo()
        emails = [rec.tasker_email for rec in self if rec.tasker_email]
        by_login, by_email = {}, {}
        if emails:
            for u in Users.search(['|', ('login', 'in', emails), ('email', 'in', emails)]):
                if u.login:
                    by_login.setdefault(u.login, u)
                if u.email:
                    by_email.setdefault(u.email, u)
        for rec in self:
            user = False
            if rec.tasker_email:
                user = by_login.get(rec.tasker_email) or by_email.get(rec.tasker_email)
            rec.tasker_user_id = user.id if user else False

    @api.depends('pl_verified_reason', 'baseline_gen_reason', 'manual_qc_reason')
    def _compute_failure_reason(self):
        """Surface the most recent stage failure reason for the Failed view."""
        for rec in self:
            rec.failure_reason = (
                rec.manual_qc_reason or rec.baseline_gen_reason
                or rec.pl_verified_reason or ''
            )

    @api.depends('status')
    def _compute_final_status(self):
        """Mirror the terminal outcome so all surfaces share one source of truth."""
        for rec in self:
            if rec.status == 'deliverable':
                rec.final_status = 'deliverable'
            elif rec.status == 'failed':
                rec.final_status = 'failed'
            else:
                rec.final_status = 'in_progress'

    @api.depends('drive_link', 'pl_verified_status', 'baseline_drive_link',
                 'baseline_gen_status', 'qced_by', 'manual_qc_status')
    def _compute_status(self):
        """Derive the current stage from the fields completed so far.

        Sequential: a stage is only reached once the previous stage's required
        fields are filled. A ``failed`` outcome at any gated stage terminates.
        """
        for rec in self:
            s = 'in_progress'
            if rec.drive_link:
                s = 'tasker_qc_completed'
            if rec.pl_verified_status == 'done':
                s = 'ready_baseline'
            if rec.pl_verified_status == 'done' and rec.baseline_drive_link:
                s = 'baseline_generated'
            if rec.baseline_gen_status == 'done' and rec.qced_by:
                s = 'manual_qc'
            if rec.manual_qc_status == 'done':
                s = 'deliverable'
            if 'failed' in (rec.pl_verified_status, rec.baseline_gen_status,
                            rec.manual_qc_status):
                s = 'failed'
            rec.status = s

    # ------------------------------------------------------------------ #
    #  Stage-gate validation (must complete a stage before progressing)
    # ------------------------------------------------------------------ #
    @api.constrains('baseline_gen_status', 'qced_by')
    def _check_baseline_metrics(self):
        for rec in self:
            if rec.baseline_gen_status == 'done' and not rec.qced_by:
                raise ValidationError(_(
                    "Enter the evaluation metrics (including QCed By) before "
                    "marking Baseline Generation as Done."))

    @api.constrains('pl_verified_status', 'pl_verified_reason',
                    'baseline_gen_status', 'baseline_gen_reason',
                    'manual_qc_status', 'manual_qc_reason')
    def _check_failure_reasons(self):
        checks = [
            ('pl_verified_status', 'pl_verified_reason', 'PL Verification'),
            ('baseline_gen_status', 'baseline_gen_reason', 'Baseline Generation'),
            ('manual_qc_status', 'manual_qc_reason', 'Manual QC'),
        ]
        for rec in self:
            for status_f, reason_f, label in checks:
                if rec[status_f] == 'failed' and not (rec[reason_f] or '').strip():
                    raise ValidationError(_(
                        "A reason is required when %s is marked Failed.", label))

    # ------------------------------------------------------------------ #
    #  Constraints — one task can be allocated only once
    # ------------------------------------------------------------------ #
    @api.constrains('task_id')
    def _check_unique_task(self):
        for rec in self:
            # sudo: uniqueness must consider records the user cannot see (record rules).
            if rec.task_id and self.sudo().search_count(
                    [('task_id', '=', rec.task_id), ('id', '!=', rec.id)]):
                raise ValidationError(_(
                    "Task '%s' is already allocated. One task can be assigned "
                    "to only one tasker at a time.", rec.task_id))

    # ------------------------------------------------------------------ #
    #  Write / create — stamp the completion datetime on terminal stages
    #  (used by the Daily Tracker as the completion date).
    # ------------------------------------------------------------------ #
    def _stamp_completion(self):
        for rec in self:
            if rec.status in ('deliverable', 'failed') and not rec.date_final:
                rec.date_final = fields.Datetime.now()

    def write(self, vals):
        res = super().write(vals)
        # status is computed; stamp completion based on the (re)computed value.
        self._stamp_completion()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._stamp_completion()
        return records
