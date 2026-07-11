# -*- coding: utf-8 -*-
import re

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, ValidationError

_TASK_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_URL_RE = re.compile(r'^https?://\S+$', re.IGNORECASE)


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
    # Only the states that _compute_status can actually derive are listed. The
    # earlier draft carried 'task_created', 'pl_verified' and 'pass1' — none of
    # which the compute ever produced — so they were dead statusbar/funnel
    # entries. Keep this list in lockstep with _compute_status.
    STATUS_SELECTION = [
        ('in_progress', 'In Progress (Authoring)'),
        ('tasker_qc_completed', 'Tasker QC Completed'),
        ('ready_baseline', 'Ready for Baseline Trajectory'),
        ('baseline_generated', 'Baseline Trajectory Generated'),
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
    # Auto-generated, unique, alphanumeric identifier (prefilled from a sequence).
    task_id = fields.Char(
        string='Task ID', required=True, index=True, copy=False, tracking=True,
        default=lambda self: self.env['ir.sequence'].next_by_code(
            'kensei.tracker.allocation'),
    )
    # The tasker is picked from the Kensei team roster (search by name or email);
    # tasker_email/name and the assigned PL/QL are all synced from that member.
    tasker_member_id = fields.Many2one(
        'kensei.tracker.team.member', string='Tasker', required=True,
        index=True, ondelete='restrict', tracking=True,
        help='Pick the tasker from the Kensei team roster (search by name or '
             'email). Their email and Assigned PL / QL are synced automatically.',
    )
    tasker_name = fields.Char(string='Tasker Name', tracking=True)
    tasker_email = fields.Char(string='Tasker Email', required=True, index=True, tracking=True)
    tasker_user_id = fields.Many2one(
        'res.users', string='Tasker User', compute='_compute_tasker_user',
        store=True, index=True,
        help='Resolved from the Tasker Email; used to scope what a tasker sees.',
    )
    tasker_id = fields.Char(string='Tasker ID', index=True, tracking=True)
    # PL/QL are synced from the selected tasker's team-member record.
    # Editable default: defaults from the tasker's roster PL, but a QL/PL may
    # override it per task. Changing the tasker re-applies the roster default.
    assigned_pl_id = fields.Many2one(
        'res.users', string='Assigned PL', compute='_compute_assignments',
        store=True, readonly=False, index=True, tracking=True,
        help="Defaults from the selected Tasker's team-member record; can be "
             "overridden per task.")
    assigned_ql_id = fields.Many2one(
        'res.users', string='Assigned QL', compute='_compute_assignments',
        store=True, readonly=True, index=True, tracking=True,
        help="Synced from the selected Tasker's team-member record.")
    # hr.employee mirror of the (possibly overridden) assigned PL — kept
    # computed so the Dashboard / Daily Tracker, which group by pl_id, keep working.
    pl_id = fields.Many2one(
        'hr.employee', string='PL Assigned', index=True, tracking=True,
        compute='_compute_pl_employee', store=True, readonly=True,
        help='Project Lead — mirrors the Assigned PL for reporting.',
    )
    # True for QL / PL / admin. Drives editability of the core allocation fields
    # in the form (Tasker, Persona, PL, Function, Assignment Date); the server
    # guard enforces the same server-side. A plain Tasker may only fill their
    # stage inputs (drive link, notes).
    is_manager = fields.Boolean(compute='_compute_is_manager')
    team_lead_id = fields.Many2one(
        'hr.employee', string='Team Lead / Manager', index=True, tracking=True,
    )
    project = fields.Char(string='Project', index=True, tracking=True)
    assigned_date = fields.Date(
        string='Assignment Date', default=fields.Date.context_today, index=True, tracking=True,
    )
    persona_id = fields.Many2one(
        'kensei.persona', string='Persona', required=True, tracking=True)
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

    # Free-form per-stage notes (always editable — collaborative context).
    tasker_qc_notes = fields.Text(string='Tasker QC Notes')
    pl_verified_notes = fields.Text(string='PL Verified Notes')
    baseline_ready_notes = fields.Text(string='Ready for Baseline Notes')
    baseline_gen_notes = fields.Text(string='Baseline Generation Notes')
    manual_qc_notes = fields.Text(string='Manual QC Notes')

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
        string='Final Status', compute='_compute_status', store=True,
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

    @api.onchange('tasker_member_id')
    def _onchange_tasker_member(self):
        """Live sync in the form: fill email/name from the picked team member
        (the assigned PL/QL recompute automatically)."""
        if self.tasker_member_id:
            self.tasker_email = self.tasker_member_id.email
            self.tasker_name = self.tasker_member_id.member_name

    def _sync_tasker(self, vals_list):
        """Keep tasker_member_id <-> tasker_email/name consistent for programmatic
        writes (the form uses the onchange above), for a whole batch of value
        dicts in at most two queries — one browse for the picked members, one
        search for the email-only rows. Called with a single-item list from
        ``write`` and the full ``vals_list`` from ``create`` so a bulk create of
        thousands of allocations never browses/searches per row (see review)."""
        Member = self.env['kensei.tracker.team.member'].sudo()

        # 1) rows that gave a member -> fetch names/emails in one browse
        member_ids = {v['tasker_member_id'] for v in vals_list if v.get('tasker_member_id')}
        members = {m.id: m for m in Member.browse(list(member_ids)).exists()}

        # 2) rows that gave only an email -> resolve the member in one search
        emails = {v['tasker_email'].strip() for v in vals_list
                  if v.get('tasker_email') and not v.get('tasker_member_id')}
        by_email = {}
        if emails:
            terms = list(emails | {e.lower() for e in emails})
            for m in Member.with_context(active_test=False).search([('email', 'in', terms)]):
                if m.email:
                    by_email[m.email.strip().lower()] = m

        for vals in vals_list:
            if vals.get('tasker_member_id'):
                m = members.get(vals['tasker_member_id'])
                if m:
                    vals.setdefault('tasker_email', m.email)
                    vals.setdefault('tasker_name', m.member_name)
            elif vals.get('tasker_email'):
                m = by_email.get(vals['tasker_email'].strip().lower())
                if m:
                    vals['tasker_member_id'] = m.id

    @api.depends('tasker_member_id',
                 'tasker_member_id.assigned_pl_id',
                 'tasker_member_id.assigned_ql_id')
    def _compute_assignments(self):
        """Default Assigned PL / QL (res.users) from the tasker's team member.
        PL is an editable default (``readonly=False``) — picking a new tasker
        re-applies the roster default, but a QL/PL may override it per task."""
        for rec in self:
            m = rec.tasker_member_id
            rec.assigned_pl_id = m.assigned_pl_id.id if (m and m.assigned_pl_id) else False
            rec.assigned_ql_id = m.assigned_ql_id.id if (m and m.assigned_ql_id) else False

    @api.depends_context('uid')
    def _compute_is_manager(self):
        allowed = (self.env.user.has_group('kensei.group_kensei_ql')
                   or self.env.user.has_group('base.group_system'))
        for rec in self:
            rec.is_manager = allowed

    @api.depends('assigned_pl_id')
    def _compute_pl_employee(self):
        """Mirror the (possibly overridden) Assigned PL onto ``pl_id``
        (hr.employee) so the Dashboard / Daily Tracker keep grouping by PL."""
        for rec in self:
            u = rec.assigned_pl_id.sudo()
            emp = (u.employee_id or u.employee_ids[:1]) if u else False
            rec.pl_id = emp.id if emp else False

    @api.constrains('task_id')
    def _check_task_id(self):
        for rec in self:
            if rec.task_id and not _TASK_ID_RE.match(rec.task_id):
                raise ValidationError(_(
                    "Task ID must be alphanumeric — letters, digits, dashes or "
                    "underscores only: “%s”.", rec.task_id))

    @api.constrains('drive_link', 'baseline_drive_link')
    def _check_drive_links(self):
        for rec in self:
            for fname, label in (('drive_link', 'Drive Link'),
                                 ('baseline_drive_link', 'Baseline Trajectory Drive Link')):
                val = (rec[fname] or '').strip()
                if val and not _URL_RE.match(val):
                    raise ValidationError(_(
                        "%s must be a valid URL starting with http:// or "
                        "https:// — got “%s”.", label, val))

    @api.depends('pl_verified_reason', 'baseline_gen_reason', 'manual_qc_reason')
    def _compute_failure_reason(self):
        """Surface the most recent stage failure reason for the Failed view."""
        for rec in self:
            rec.failure_reason = (
                rec.manual_qc_reason or rec.baseline_gen_reason
                or rec.pl_verified_reason or ''
            )

    @api.depends('drive_link', 'pl_verified_status', 'baseline_drive_link',
                 'baseline_gen_status', 'qced_by', 'manual_qc_status')
    def _compute_status(self):
        """Derive the current stage (and mirrored final status) from the fields
        completed so far.

        Sequential: a stage is only reached once the previous stage's required
        fields are filled. A ``failed`` outcome at any gated stage terminates.
        ``final_status`` is derived in the same pass so every surface shares one
        source of truth without a second (chained) recompute.
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
            rec.final_status = s if s in ('deliverable', 'failed') else 'in_progress'

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
    #  Constraints — one task can be allocated only once.
    #  Enforced at the database level: atomic and race-safe (a per-record
    #  search_count could let two concurrent creates both pass), and global —
    #  it also catches duplicates hidden from the current user by record rules,
    #  which the previous sudo()-search approach only papered over.
    # ------------------------------------------------------------------ #
    # Odoo 19 replaces the old ``_sql_constraints`` list with models.Constraint.
    _task_id_uniq = models.Constraint(
        'unique(task_id)',
        'This Task ID is already allocated. One task can be assigned to '
        'only one tasker at a time.',
    )

    # ------------------------------------------------------------------ #
    #  Write / create — stamp the completion datetime on terminal stages
    #  (used by the Daily Tracker as the completion date).
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    #  Restricted (QL/PL-only) fields — enforced server-side (UI ``readonly`` is
    #  only cosmetic). Taskers may fill *all* stage data (Tasker-QC, PL
    #  verification, trajectory, scoring and Manual QC) on their own record; they
    #  just cannot reassign the task itself (tasker/persona/PL/function/date).
    # ------------------------------------------------------------------ #
    _PRIVILEGED_STAGE_STATES = {}
    _PRIVILEGED_VALUE_FIELDS = (
        # core allocation fields — only a QL/PL may set/change these
        'tasker_member_id', 'persona_id', 'assigned_pl_id',
        'function', 'assigned_date',
    )

    def _guard_privileged_fields(self, vals):
        """Block non-QL users from changing the core assignment fields (tasker,
        persona, PL, function, assignment date). Stage/QC fields are intentionally
        left open to taskers so they can drive their task end to end.
        """
        env = self.env
        user = env.user
        if (env.su or user.has_group('kensei.group_kensei_ql')
                or user.has_group('base.group_system')):
            return
        blocked = [f for f, states in self._PRIVILEGED_STAGE_STATES.items()
                   if vals.get(f) in states]
        blocked += [f for f in self._PRIVILEGED_VALUE_FIELDS if vals.get(f)]
        if blocked:
            raise AccessError(_(
                "Only a Project Lead / QL can re-assign a task (tasker, persona, "
                "PL, function or assignment date). Restricted field(s): %s",
                ", ".join(sorted(set(blocked)))))

    def _stamp_completion(self):
        """Stamp the completion datetime when a task enters a terminal stage,
        and clear it again if the task later reverts to an in-progress stage —
        so the Daily Tracker never attributes a completion to a stale date.
        """
        now = fields.Datetime.now()
        for rec in self:
            if rec.status in ('deliverable', 'failed'):
                if not rec.date_final:
                    rec.date_final = now
            elif rec.date_final:
                rec.date_final = False

    def write(self, vals):
        self._guard_privileged_fields(vals)
        self._sync_tasker([vals])
        res = super().write(vals)
        # status is computed; stamp completion based on the (re)computed value.
        self._stamp_completion()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._guard_privileged_fields(vals)
        self._sync_tasker(vals_list)
        records = super().create(vals_list)
        records._stamp_completion()
        return records
