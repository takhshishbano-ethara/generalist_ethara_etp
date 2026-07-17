# -*- coding: utf-8 -*-
import re
import uuid

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError

# A Task ID is a canonical UUID: 8-4-4-4-12 lowercase hex.
#     123e4567-e89b-12d3-a456-426614174000
# Anchored and case-insensitive on input; _normalise_task_id() lowercases it, so
# the stored form is always canonical and two IDs can never differ only by case.
_TASK_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
# A link must have a real dotted host (something.tld), not just a scheme — so
# "https://g" is rejected while "https://drive.google.com/…" is accepted.
_URL_RE = re.compile(
    r'^https?://[^\s/?#]+\.[^\s/?#]{2,}([/?#]\S*)?$', re.IGNORECASE)

# Each stage of a task lives in its OWN record, so a single form can only reach the
# other stage's data through the mirror fields (s1_* / s2_*). These tuples are the
# one source of truth for which pipeline fields are mirrored: the field list, the
# @api.depends and the compute all derive from them, so they cannot drift apart.
_STAGE1_MIRROR = (
    'status',
    # Each stage carries its OWN PL/QL, synced from that stage's tasker on the roster,
    # so the two stages can sit under different leads. Mirrored so the form can show
    # each tasker next to the lead who actually owns them.
    'assigned_pl_id', 'assigned_ql_id',
    'drive_link', 'tasker_qc_notes',
    'pl_verified_status', 'pl_verified_reason', 'pl_verified_notes',
    'baseline_ready_status', 'baseline_ready_reason', 'baseline_ready_notes',
    'baseline_drive_link', 'baseline_gen_status', 'baseline_gen_reason',
    'baseline_gen_notes',
    'rubric_score', 'pytest_score', 'overall_score', 'qced_by',
    'manual_qc_status', 'manual_qc_reason', 'manual_qc_notes',
    'date_final',
)
# Stage 2 inherits a task that stage 1 already authored, PL-verified and signed off
# as ready, so it runs a shorter pipeline: generate the baseline trajectory, QC it.
_STAGE2_MIRROR = (
    'status',
    'assigned_pl_id', 'assigned_ql_id',
    'baseline_drive_link', 'baseline_gen_status', 'baseline_gen_reason',
    'baseline_gen_notes',
    'rubric_score', 'pytest_score', 'overall_score', 'qced_by',
    'manual_qc_status', 'manual_qc_reason', 'manual_qc_notes',
    'date_final',
)


def _mirror_value(source, name):
    """Read ``name`` off a stage record, flattening a Many2one to its id."""
    if not source:
        return False
    value = source[name]
    return value.id if isinstance(value, models.BaseModel) else value


class Kensei2TrackerAllocation(models.Model):
    """A task assigned to a tasker, tracked through the RL pipeline.

    The workflow status bar drives which stage inputs are shown/required on the
    form. Inherits mail.thread so every tracked change is logged with who + when.
    """
    _name = 'project.tracker.allocation'
    _description = 'Project Tracker Task Allocation'
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
    # The LABELS are deliberately short. They are the stage names in the form's
    # status bar, and core's StatusBarField.adjustVisibleItems() runs
    #     while (this.areItemsWrapping()) { ...hide items into a "…" dropdown... }
    # so the moment the row wraps, stages get FOLDED AWAY — which is exactly the
    # "stage names are cut off / not fully visible" report. The old labels
    # ("In Progress (Authoring)", "Ready for Baseline Trajectory", "Baseline
    # Trajectory Generated", …) came to ~129 characters across six stages, which
    # cannot fit one row at any sane width, so the bar always folded.
    #
    # Keep these SHORT. The stored keys are unchanged, so nothing else moves.
    STATUS_SELECTION = [
        ('in_progress', 'Authoring'),
        ('tasker_qc_completed', 'Tasker QC'),
        ('ready_baseline', 'Ready for Baseline'),
        ('baseline_generated', 'Baseline Generated'),
        ('manual_qc', 'Stage 1 Manual QC'),
        # Finishing the pipeline does NOT mean the task is delivered: a task runs
        # over several stages. A non-final stage lands here — its work is done and
        # it is waiting to be handed off to the next stage. Only the FINAL stage
        # can reach 'deliverable'.
        ('ready_next_stage', 'Ready for Next Stage'),
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
    # Auto-generated, alphanumeric identifier (prefilled from a sequence). NOT
    # unique on its own: one task runs through several stages, and each stage is
    # its own allocation record sharing the same Task ID (unique per stage).
    task_id = fields.Char(
        string='Task ID', required=True, index=True, copy=False, tracking=True,
        default=lambda self: self._new_task_uuid(),
        help='Canonical UUID (8-4-4-4-12). Every stage of the SAME task shares '
             'this ID — it is what chains them together — so it is unique per '
             '(Task ID, Stage), not per row.',
    )


    # ------------------------------------------------------------------ #
    #  Stage 2 speaks its own dialect
    # ------------------------------------------------------------------ #
    # Both stages run the same pipeline over the same fields and the same STORED
    # values — but they name the trajectory step differently: stage 1 calls it
    # Baseline, stage 2 calls it Pass @ K. A Selection carries exactly ONE set of
    # labels, so `status` holds stage 1's and this holds stage 2's. Same values,
    # different words. Nothing extra is stored; there is no second source of truth.
    STAGE2_STATUS_SELECTION = [
        ('in_progress', 'Authoring'),
        ('tasker_qc_completed', 'Tasker QC'),
        ('ready_baseline', 'Ready for Pass @ K'),
        ('baseline_generated', 'Pass @ K Generated'),
        ('manual_qc', 'Stage 2 Manual QC'),
        ('ready_next_stage', 'Ready for Next Stage'),
        ('deliverable', 'Deliverable'),
        ('failed', 'Failed'),
    ]
    stage2_status = fields.Selection(
        selection=STAGE2_STATUS_SELECTION, string='Current Status',
        compute='_compute_stage2_status',
        help="The same Current Status, in stage 2's words (Pass @ K, not Baseline).",
    )

    @api.depends('status')
    def _compute_stage2_status(self):
        for rec in self:
            rec.stage2_status = rec.status

    # The list and the kanban show BOTH stages in one table, so a single Selection
    # label is always wrong for half the rows. This picks the right one PER ROW.
    # Stored, so it can be sorted and grouped like any other column.
    status_label = fields.Char(
        string='Current Status', compute='_compute_status_label',
        store=True, index=True,
    )

    @api.depends('status', 'stage_no')
    def _compute_status_label(self):
        s1 = dict(self.STATUS_SELECTION)
        s2 = dict(self.STAGE2_STATUS_SELECTION)
        for rec in self:
            table = s2 if rec.stage_no and rec.stage_no >= 2 else s1
            rec.status_label = table.get(rec.status, rec.status or '')

    @api.depends('task_id', 'stage_no')
    def _compute_display_name(self):
        """Show the STAGE alongside the Task ID.

        Every stage of a task shares the task_id — that shared ID is what chains
        them — and _rec_name is task_id, so without this BOTH stages display the
        exact same string. The "Handed off from …" link on stage 2 then shows the
        very UUID the record itself is showing, which reads as if the task were
        handed off from itself. Naming the stage is what makes the link mean
        something.
        """
        for rec in self:
            if rec.task_id and rec.stage_no:
                rec.display_name = "%s (Stage %s)" % (rec.task_id, rec.stage_no)
            else:
                rec.display_name = rec.task_id or _("New Task")

    @api.model
    def _new_task_uuid(self):
        """A UUID that is not already in use by another task.

        uuid4 collisions are, in practice, impossible — but "in practice" is not
        the same as "the database will refuse it". A duplicate here would not raise
        a unique-constraint error either: the key is (task_id, stage_no), so a fresh
        stage-1 record colliding with an EXISTING stage-2 record would insert
        cleanly and silently FUSE two unrelated tasks into one chain. That is
        unrecoverable data corruption, so it is worth one indexed SELECT to rule out.

        sudo(): the check must see the whole table. Under the team-scoped record
        rules a lead cannot see other teams' rows, and a collision check that cannot
        see the row it is meant to detect is worthless.
        """
        Alloc = self.sudo()
        for _attempt in range(5):
            candidate = str(uuid.uuid4())
            if not Alloc.search_count([('task_id', '=', candidate)]):
                return candidate
        # Five uuid4 collisions in a row is not luck, it is a broken RNG.
        raise ValidationError(_(
            "Could not generate a unique Task ID. This should be impossible — "
            "check the system's random number generator."))

    # ---------------- Stage chain -------------------------------------- #
    # A task is worked in stages. Each stage is a SEPARATE allocation record so
    # it gets its own tasker, PL/QL, pipeline status, scores and audit trail —
    # the whole existing pipeline is simply replayed. Stage N+1 is created from
    # stage N via the hand-off wizard once stage N reaches Deliverable.
    stage_no = fields.Integer(
        string='Stage', default=1, required=True, index=True, tracking=True,
        help='Which stage of the task this allocation covers (1, 2, …).',
    )
    # How many stages this task takes end to end. Drives what "finished" means:
    # completing a NON-final stage makes the task "Ready for Next Stage"; only the
    # final stage can become "Deliverable". Carried forward on hand-off.
    # Fixed at 2 and not an input: a task is authored + verified in stage 1, then its
    # baseline trajectory is generated + QC'd in stage 2. ``readonly`` blocks the UI
    # only, so the hand-off wizard's ORM create still carries it forward.
    total_stages = fields.Integer(
        string='Total Stages', default=2, required=True, readonly=True, tracking=True,
        help='A task always runs through exactly two stages. Only stage 2 can reach '
             'Deliverable.',
    )
    is_final_stage = fields.Boolean(
        compute='_compute_is_final_stage', store=True,
        help='True when this is the last stage of the task — the only stage that '
             'can deliver.',
    )
    parent_id = fields.Many2one(
        'project.tracker.allocation', string='Previous Stage',
        ondelete='restrict', index=True, copy=False,
        help='The allocation of the preceding stage this one was handed off from.',
    )
    child_ids = fields.One2many(
        'project.tracker.allocation', 'parent_id', string='Next Stages')
    # "Handed off from …" needs to name the PERSON and the STAGE. Rendering parent_id
    # itself showed its display_name — which is built from the task_id every stage
    # shares, i.e. the same string the title already shows. Useless. These two say
    # what the reader actually wants: which stage, and who worked it.
    parent_stage_no = fields.Integer(
        related='parent_id.stage_no', string='Previous Stage', readonly=True)
    parent_tasker_id = fields.Many2one(
        related='parent_id.tasker_member_id', string='Previous Tasker', readonly=True)

    def action_open_parent(self):
        """Open the stage this one was handed off from."""
        self.ensure_one()
        if not self.parent_id:
            raise UserError(_("This stage was not handed off from another one."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stage %s', self.parent_id.stage_no),
            'res_model': 'project.tracker.allocation',
            'res_id': self.parent_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
    # True when this is the LATEST stage of its task (nothing handed off from it
    # yet). The Dashboard filters on this so a multi-stage task is counted ONCE,
    # at the stage it currently sits in, instead of once per stage record.
    is_current_stage = fields.Boolean(
        string='Current Stage', compute='_compute_is_current_stage',
        store=True, index=True, default=True,
    )
    # Drives the "Hand off to next stage" button: only a completed (Deliverable)
    # stage that has not been handed off yet can start the next one. A Failed
    # stage is terminal — no hand-off.
    can_hand_off = fields.Boolean(compute='_compute_can_hand_off')
    # A finished stage's inputs are frozen: everything downstream (the next
    # stage's baseline, the delivery credit, the dashboards) was derived from
    # them, so editing them in place would silently invalidate that work. Getting
    # back in is an explicit, audited act -- see action_reopen.
    is_locked = fields.Boolean(
        string='Locked', compute='_compute_is_locked',
        help='Finished (Deliverable/Failed) or already handed off to a next '
             'stage, so its inputs can no longer be edited in place.',
    )
    # A SECOND, softer lock for the failure-documentation fields (the reason/notes at
    # each gate). Marking a gate Failed drives the whole record to status 'failed',
    # which sets is_locked — and that froze the very reason field the user must fill
    # to explain the failure (it is `required` and only visible WHEN failed, so the
    # form demanded a reason it would not let you type, and the tab looked frozen).
    # These fields therefore lock one step later than the rest: only on Deliverable
    # (a success terminal) or hand-off, never merely on Failed.
    is_doc_locked = fields.Boolean(
        string='Documentation Locked', compute='_compute_is_locked',
        help='Failure reason/notes are frozen (delivered, or handed off). The '
             'Failed state alone does NOT freeze them, so a failure can be '
             'documented after it happens.',
    )

    # When the CURRENT status was entered. Stamped whenever `status` changes (see
    # write/create), so the form can answer "how long has this been stuck here?" —
    # the question a lead actually opens the record to ask.
    status_since = fields.Datetime(
        string='In Status Since', readonly=True, copy=False)
    days_in_status = fields.Integer(
        string='Days in Status', compute='_compute_days_in_status',
        help='Whole days the task has sat in its current step.')

    @api.depends('status_since')
    def _compute_days_in_status(self):
        # Not stored: it grows with wall-clock time, so it is derived on read.
        now = fields.Datetime.now()
        for rec in self:
            rec.days_in_status = (
                (now - rec.status_since).days if rec.status_since else 0)

    # A record only ever holds its OWN tasker, so these are what let any one stage
    # show who works the others: the whole chain (every record sharing the Task ID,
    # oldest first) plus stage 1's and stage 2's tasker surfaced directly. Not
    # stored — a sibling stage can be created or reassigned at any time.
    stage_ids = fields.Many2many(
        'project.tracker.allocation', string='All Stages',
        compute='_compute_stage_chain',
    )
    stage1_tasker_id = fields.Many2one(
        'project.tracker.team.member', string='Stage 1 Tasker',
        compute='_compute_stage_chain',
    )
    stage2_tasker_id = fields.Many2one(
        'project.tracker.team.member', string='Stage 2 Tasker',
        compute='_compute_stage_chain',
    )

    # ---------------- Cross-stage mirrors ------------------------------ #
    # Whether this task has a Stage 2, resolved from the chain relations
    # (parent_id / child_ids) rather than a search on task_id, so Odoo can track the
    # dependency and refresh the flag when the sibling stage changes.
    has_stage_2 = fields.Boolean(
        string='Stage 2 Assigned', compute='_compute_stage_targets',
    )

    # Stage 1's pipeline, projected onto this record so one form can show both stages.
    s1_status = fields.Selection(
        selection=STATUS_SELECTION, string='Stage 1 Status',
        compute='_compute_stage_mirrors')
    s1_assigned_pl_id = fields.Many2one(
        'res.users', string='PL', compute='_compute_stage_mirrors')
    s1_assigned_ql_id = fields.Many2one(
        'res.users', string='QL', compute='_compute_stage_mirrors')
    s1_drive_link = fields.Char(
        string='Drive Link', compute='_compute_stage_mirrors')
    s1_tasker_qc_notes = fields.Text(
        string='Tasker QC Notes', compute='_compute_stage_mirrors')
    s1_pl_verified_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='PL Verification',
        compute='_compute_stage_mirrors')
    s1_pl_verified_reason = fields.Text(
        string='PL Verification Reason', compute='_compute_stage_mirrors')
    s1_pl_verified_notes = fields.Text(
        string='PL Verified Notes', compute='_compute_stage_mirrors')
    s1_baseline_ready_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Ready for Baseline Trajectory',
        compute='_compute_stage_mirrors')
    s1_baseline_ready_reason = fields.Text(
        string='Ready for Baseline Reason', compute='_compute_stage_mirrors')
    s1_baseline_ready_notes = fields.Text(
        string='Ready for Baseline Notes', compute='_compute_stage_mirrors')
    s1_baseline_drive_link = fields.Char(
        string='Baseline Trajectory Drive Link', compute='_compute_stage_mirrors')
    s1_baseline_gen_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Baseline Generation',
        compute='_compute_stage_mirrors')
    s1_baseline_gen_reason = fields.Text(
        string='Baseline Generation Reason', compute='_compute_stage_mirrors')
    s1_baseline_gen_notes = fields.Text(
        string='Baseline Generation Notes', compute='_compute_stage_mirrors')
    s1_rubric_score = fields.Float(
        string='Rubric Score (%)', compute='_compute_stage_mirrors')
    s1_pytest_score = fields.Float(
        string='Pytest Score (%)', compute='_compute_stage_mirrors')
    s1_overall_score = fields.Float(
        string='Overall Score (%)', compute='_compute_stage_mirrors')
    s1_qced_by = fields.Many2one(
        'res.users', string='QCed By', compute='_compute_stage_mirrors')
    s1_manual_qc_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Manual QC Result',
        compute='_compute_stage_mirrors')
    s1_manual_qc_reason = fields.Text(
        string='Manual QC Reason', compute='_compute_stage_mirrors')
    s1_manual_qc_notes = fields.Text(
        string='Manual QC Notes', compute='_compute_stage_mirrors')
    s1_date_final = fields.Datetime(
        string='Stage 1 Completed On', compute='_compute_stage_mirrors')

    # Stage 2's shorter pipeline (baseline trajectory + manual QC only).
    s2_status = fields.Selection(
        selection=STAGE2_STATUS_SELECTION, string='Stage 2 Status',
        compute='_compute_stage_mirrors')
    s2_assigned_pl_id = fields.Many2one(
        'res.users', string='PL', compute='_compute_stage_mirrors')
    s2_assigned_ql_id = fields.Many2one(
        'res.users', string='QL', compute='_compute_stage_mirrors')
    s2_baseline_drive_link = fields.Char(
        string='Baseline Trajectory Drive Link', compute='_compute_stage_mirrors')
    s2_baseline_gen_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Baseline Generation',
        compute='_compute_stage_mirrors')
    s2_baseline_gen_reason = fields.Text(
        string='Baseline Generation Reason', compute='_compute_stage_mirrors')
    s2_baseline_gen_notes = fields.Text(
        string='Baseline Generation Notes', compute='_compute_stage_mirrors')
    s2_rubric_score = fields.Float(
        string='Rubric Score (%)', compute='_compute_stage_mirrors')
    s2_pytest_score = fields.Float(
        string='Pytest Score (%)', compute='_compute_stage_mirrors')
    s2_overall_score = fields.Float(
        string='Overall Score (%)', compute='_compute_stage_mirrors')
    s2_qced_by = fields.Many2one(
        'res.users', string='QCed By', compute='_compute_stage_mirrors')
    s2_manual_qc_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Manual QC Result',
        compute='_compute_stage_mirrors')
    s2_manual_qc_reason = fields.Text(
        string='Manual QC Reason', compute='_compute_stage_mirrors')
    s2_manual_qc_notes = fields.Text(
        string='Manual QC Notes', compute='_compute_stage_mirrors')
    s2_date_final = fields.Datetime(
        string='Stage 2 Completed On', compute='_compute_stage_mirrors')

    # The tasker is picked from the Kensei2 team roster (search by name or email);
    # tasker_email/name and the assigned PL/QL are all synced from that member.
    tasker_member_id = fields.Many2one(
        'project.tracker.team.member', string='Tasker', required=True,
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
    project_id = fields.Many2one(
        'project.tracker.project', string='Project', index=True, tracking=True,
        ondelete='restrict',
        help='Which project this task belongs to — the Tracker serves every '
             'project, this is the axis that keeps them apart.')
    # Kept as a stored related so everything that filtered/grouped on the old
    # free-text column (Daily Tracker filters, exports) keeps working unchanged.
    project = fields.Char(
        string='Project Name', related='project_id.name', store=True, index=True)
    assigned_date = fields.Date(
        string='Assignment Date', default=fields.Date.context_today, index=True, tracking=True,
    )
    persona_id = fields.Many2one(
        'kensei2.persona', string='Persona', required=True, index=True, tracking=True)
    # Surfaced from the chosen persona so the form shows its taxonomy next to it.
    persona_l1_category = fields.Char(
        related='persona_id.l1_category', string='L1 Category', readonly=True)
    persona_l2_category = fields.Char(
        related='persona_id.l2_category', string='L2 Category', readonly=True)
    # Derived from the current Status (the pipeline phase) rather than set by
    # hand, so it can never drift from where the task actually is. Terminal
    # statuses (Deliverable/Failed) map to Manual QC — the last phase reached.
    function = fields.Selection(
        selection=[
            ('task_authoring', 'Task Authoring'),
            ('trajectory', 'Trajectory'),
            ('manual_qc', 'Manual QC'),
        ],
        string='Function', compute='_compute_function', store=True,
        readonly=True, index=True, tracking=True,
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

    # Picker restrictions: qced_by and the PL override are res.users, which would
    # otherwise list EVERY Odoo user. Scope them to the Tracker roster so only
    # relevant people appear. Non-stored (the roster changes), computed once and
    # broadcast to the recordset since the eligible set is the same for every task.
    # Existing values that fall outside the set still display — a domain only filters
    # what the dropdown OFFERS, it never invalidates a stored value.
    qc_candidate_ids = fields.Many2many(
        'res.users', compute='_compute_pick_candidates',
        string='QC Candidates',
        help='Active team members — the only users selectable as QCed By.')
    pl_candidate_ids = fields.Many2many(
        'res.users', compute='_compute_pick_candidates',
        string='PL Candidates',
        help='Active team members who are leads (PL / QL / Admin).')

    @api.depends_context('uid')
    def _compute_pick_candidates(self):
        Member = self.env['project.tracker.team.member'].sudo()
        active = Member.search([('status', '=', 'active'),
                                ('user_id', '!=', False)])
        qc_users = active.user_id
        pl_users = active.filtered(
            lambda m: m.role in ('pl', 'ql', 'admin')).user_id
        for rec in self:
            rec.qc_candidate_ids = qc_users
            rec.pl_candidate_ids = pl_users

    # ------------------------------------------------------------------ #
    #  Stage-specific inputs (shown dynamically on the form by status)
    # ------------------------------------------------------------------ #
    drive_link = fields.Char(string='Drive Link', tracking=True)
    pl_verified_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='PL Verification',
        default='in_progress', tracking=True,
    )
    pl_verified_reason = fields.Text(string='PL Verification Reason')
    baseline_ready_status = fields.Selection(
        selection=STAGE_STATUS_SELECTION, string='Ready for Baseline Trajectory',
        default='in_progress', tracking=True,
    )
    baseline_ready_reason = fields.Text(string='Ready for Baseline Reason')
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

    # Per-stage notes. Each one unlocks with its own stage (the form gates them on
    # the preceding stage) so a stage cannot be annotated before it is reachable.
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
        Member = self.env['project.tracker.team.member'].sudo()

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
        allowed = (self.env.user.has_group('project_tracker.group_project_tracker_ql')
                   or self.env.user.has_group('base.group_system'))
        for rec in self:
            rec.is_manager = allowed

    @api.depends('assigned_pl_id')
    def _compute_pl_employee(self):
        """Mirror the (possibly overridden) Assigned PL onto ``pl_id``
        (hr.employee) so the Dashboard / Daily Tracker keep grouping by PL.

        Resolved in ONE query for the whole batch. The per-record version issued a
        sudo() browse + employee lookup per row, which is an N+1 on any bulk create
        (the bulk-allocation wizard creates thousands of rows in a single call).
        """
        pl_ids = [pl.id for pl in self.mapped('assigned_pl_id')]
        emp_by_user = {}
        if pl_ids:
            for emp in self.env['hr.employee'].sudo().search(
                    [('user_id', 'in', pl_ids)]):
                emp_by_user.setdefault(emp.user_id.id, emp.id)
        for rec in self:
            rec.pl_id = emp_by_user.get(rec.assigned_pl_id.id, False)

    @api.constrains('task_id')
    def _check_task_id(self):
        for rec in self:
            if rec.task_id and not _TASK_ID_RE.match(rec.task_id):
                raise ValidationError(_(
                    "Task ID must be a UUID in the standard 8-4-4-4-12 form, "
                    "e.g. 123e4567-e89b-12d3-a456-426614174000 — got “%s”.",
                    rec.task_id))

    @api.constrains('drive_link', 'baseline_drive_link')
    def _check_drive_links(self):
        for rec in self:
            for fname, label in (('drive_link', 'Drive Link'),
                                 ('baseline_drive_link', 'Baseline Trajectory Drive Link')):
                val = (rec[fname] or '').strip()
                if val and not _URL_RE.match(val):
                    raise ValidationError(_(
                        "%s must be a full URL with a domain, e.g. "
                        "https://drive.google.com/… — got “%s”.", label, val))

    @api.depends('pl_verified_reason', 'baseline_ready_reason',
                 'baseline_gen_reason', 'manual_qc_reason')
    def _compute_failure_reason(self):
        """Surface the most recent stage failure reason for the Failed view."""
        for rec in self:
            rec.failure_reason = (
                rec.manual_qc_reason or rec.baseline_gen_reason
                or rec.baseline_ready_reason or rec.pl_verified_reason or ''
            )

    # Pipeline phase (Function) for each Status. Terminal statuses map to the
    # last active phase (Manual QC).
    _FUNCTION_BY_STATUS = {
        'in_progress': 'task_authoring',
        'tasker_qc_completed': 'task_authoring',
        'ready_baseline': 'trajectory',
        'baseline_generated': 'trajectory',
        'manual_qc': 'manual_qc',
        'ready_next_stage': 'manual_qc',
        'deliverable': 'manual_qc',
        'failed': 'manual_qc',
    }

    @api.depends('status')
    def _compute_function(self):
        for rec in self:
            rec.function = self._FUNCTION_BY_STATUS.get(rec.status, 'task_authoring')

    @api.depends('stage_no', 'drive_link', 'pl_verified_status',
                 'baseline_ready_status', 'baseline_drive_link',
                 'baseline_gen_status', 'qced_by', 'manual_qc_status',
                 'rubric_score', 'pytest_score', 'overall_score',
                 'is_final_stage')
    def _compute_status(self):
        """Derive the current stage (and mirrored final status) from the fields
        completed so far.

        STRICTLY sequential: the rungs are NESTED, so every rung re-checks the
        ones below it. Clearing or reverting an earlier input therefore drops the
        status back down to wherever the chain now breaks -- an already-delivered
        task whose Drive Link is emptied stops being Deliverable instead of
        silently keeping the credit. (A flat series of independent ``if``s, which
        this used to be, let the last rung win regardless of the ones below it.)
        A ``failed`` outcome at any gated stage still terminates, and is checked
        last so it overrides whatever the ladder reached.
        ``final_status`` is derived in the same pass so every surface shares one
        source of truth without a second (chained) recompute.

        The two stages run DIFFERENT pipelines. Stage 1 authors the task and gets it
        verified (Tasker QC -> PL Verification -> Ready for Pass @ K). Stage 2 picks
        that finished task up, so those three steps are already behind it: it starts
        at 'ready_baseline' and only generates the trajectory and manual-QCs it.
        """
        for rec in self:
            if rec.stage_no and rec.stage_no >= 2:
                s = 'ready_baseline'
                if rec.baseline_drive_link:
                    s = 'baseline_generated'
                    if (rec.baseline_gen_status == 'done' and rec.qced_by
                            and 0 < rec.rubric_score <= 100
                            and 0 < rec.pytest_score <= 100
                            and 0 < rec.overall_score <= 100):
                        s = 'manual_qc'
                        if rec.manual_qc_status == 'done':
                            s = ('deliverable' if rec.is_final_stage
                                 else 'ready_next_stage')
                if 'failed' in (rec.baseline_gen_status, rec.manual_qc_status):
                    s = 'failed'
                rec.status = s
                rec.final_status = (
                    s if s in ('deliverable', 'failed') else 'in_progress')
                continue

            s = 'in_progress'
            if rec.drive_link:
                s = 'tasker_qc_completed'
                # Ready for Baseline is an explicit sign-off of its own: the PL
                # first verifies the authored task, and only then is the stage
                # declared ready for a baseline trajectory. Both must be Done, so
                # marking either one alone can never advance the task.
                if (rec.pl_verified_status == 'done'
                        and rec.baseline_ready_status == 'done'):
                    s = 'ready_baseline'
                    # The baseline link belongs to the Baseline Trajectory stage,
                    # which only unlocks once that sign-off is in.
                    if rec.baseline_drive_link:
                        s = 'baseline_generated'
                        if (rec.baseline_gen_status == 'done' and rec.qced_by
                                and 0 < rec.rubric_score <= 100
                                and 0 < rec.pytest_score <= 100
                                and 0 < rec.overall_score <= 100):
                            s = 'manual_qc'
                            if rec.manual_qc_status == 'done':
                                # Completing the pipeline only DELIVERS the task
                                # on its final stage. On any earlier stage the
                                # task is not delivered -- it is done with this
                                # stage and ready to hand to the next one.
                                s = ('deliverable' if rec.is_final_stage
                                     else 'ready_next_stage')
            if 'failed' in (rec.pl_verified_status, rec.baseline_ready_status,
                            rec.baseline_gen_status, rec.manual_qc_status):
                s = 'failed'
            rec.status = s
            # The TASK is only finished at 'deliverable' (final stage) or 'failed'.
            # 'ready_next_stage' means more work is coming, so the task is still
            # in progress overall.
            rec.final_status = s if s in ('deliverable', 'failed') else 'in_progress'

    # ------------------------------------------------------------------ #
    #  Stage-gate validation (must complete a stage before progressing)
    # ------------------------------------------------------------------ #
    # The three evaluation scores, with their labels, in display order.
    _SCORE_FIELDS = (
        ('rubric_score', 'Rubric Score'),
        ('pytest_score', 'Pytest Score'),
        ('overall_score', 'Overall Score'),
    )

    @api.constrains('baseline_gen_status', 'qced_by',
                    'rubric_score', 'pytest_score', 'overall_score')
    def _check_baseline_metrics(self):
        for rec in self:
            # Range check runs ALWAYS, so a fat-fingered 950 or a negative is caught
            # the moment it is entered, not only at the Done gate.
            for fname, label in self._SCORE_FIELDS:
                v = rec[fname]
                if not (0.0 <= v <= 100.0):
                    raise ValidationError(_(
                        "%s must be between 0 and 100 — got %s.", label, v))
            if rec.baseline_gen_status != 'done':
                continue
            # Done means the trajectory has been evaluated, so QCed By and all three
            # scores are required. A score left at 0 is treated as NOT YET ENTERED:
            # the fields default to 0.0 and there is no way to tell a real 0 from an
            # empty one, and a trajectory scored 0 across the board would not be
            # marked Done in the first place.
            if not rec.qced_by:
                raise ValidationError(_(
                    "Enter QCed By before marking Generation as Done."))
            missing = [label for fname, label in self._SCORE_FIELDS if not rec[fname]]
            if missing:
                raise ValidationError(_(
                    "Enter the evaluation metrics (%s) before marking Generation "
                    "as Done.", ', '.join(missing)))

    @api.constrains('manual_qc_status', 'baseline_gen_status', 'qced_by',
                    'rubric_score', 'pytest_score', 'overall_score')
    def _check_manual_qc_prerequisites(self):
        """Manual QC can only be signed off (Done/Failed) once its trajectory is
        fully generated, scored and QCed — the same bar the status ladder uses to
        REACH Manual QC. Now that the tab is editable out of order (validate on
        save), this is what makes a premature Manual QC fail with a message naming
        exactly what is missing, instead of silently doing nothing."""
        for rec in self:
            if rec.manual_qc_status == 'in_progress':
                continue
            missing = []
            if rec.baseline_gen_status != 'done':
                missing.append('Generation = Done')
            if not rec.qced_by:
                missing.append('QCed By')
            missing += [label for fname, label in self._SCORE_FIELDS
                        if not (0 < rec[fname] <= 100)]
            if missing:
                raise ValidationError(_(
                    "Complete the trajectory step before Manual QC — still needed: "
                    "%s.", ', '.join(missing)))


    # ------------------------------------------------------------------ #
    #  Constraints — one task can be allocated only once.
    #  Enforced at the database level: atomic and race-safe (a per-record
    #  search_count could let two concurrent creates both pass), and global —
    #  it also catches duplicates hidden from the current user by record rules,
    #  which the previous sudo()-search approach only papered over.
    # ------------------------------------------------------------------ #
    # Odoo 19 replaces the old ``_sql_constraints`` list with models.Constraint.
    # A task now runs through stages, each its own record, so the key is
    # (task_id, stage_no): one tasker per task PER STAGE, and a stage can never
    # be allocated twice.
    _task_id_stage_uniq = models.Constraint(
        'unique(task_id, stage_no)',
        'This task is already allocated for that stage. One task can be '
        'assigned to only one tasker per stage.',
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
    _PRIVILEGED_VALUE_FIELDS = (
        # core allocation fields — only a QL/PL may set/change these
        'tasker_member_id', 'persona_id', 'assigned_pl_id', 'assigned_date',
        # tasker_email/tasker_name are the SAME control surface as
        # tasker_member_id: _sync_tasker resolves an email back to a member and
        # writes tasker_member_id from it. Leaving them out let a tasker re-assign
        # their own task to somebody else by writing an email — passing the guard,
        # which never saw a protected key. Guard the derivation, not just the
        # derived field.
        'tasker_email', 'tasker_name',
        # the stage chain is set by the hand-off wizard, never by a tasker — and
        # total_stages decides which stage may DELIVER, so it must not be
        # self-served either.
        'stage_no', 'parent_id', 'total_stages',
    )

    def _is_tracker_manager(self):
        """QL (implied by PL/Admin) or an Odoo sysadmin."""
        user = self.env.user
        return (user.has_group('project_tracker.group_project_tracker_ql')
                or user.has_group('base.group_system'))

    def _guard_privileged_fields(self, vals):
        """Block non-QL users from changing the core assignment fields (tasker,
        persona, PL, assignment date, stage chain). Stage/QC fields are
        intentionally left open to taskers so they can drive their task end to end.

        Membership is tested with ``f in vals``, NOT ``vals.get(f)``. A truthiness
        test silently let every falsy value through, and the falsy values are the
        dangerous ones: ``{'total_stages': 0}`` passed the guard and made
        ``is_final_stage`` (stage_no >= total_stages) True on stage 1, so a tasker
        could mark their own stage 'deliverable' instead of 'ready_next_stage' —
        self-certifying delivery and skipping stage 2 entirely. ``required=True``
        does not save us: Odoo does not reject 0 for an Integer.
        """
        if self.env.su or self._is_tracker_manager():
            return
        blocked = [f for f in self._PRIVILEGED_VALUE_FIELDS if f in vals]
        if blocked:
            raise AccessError(_(
                "Only a Project Lead / QL can re-assign a task (tasker, persona, "
                "PL or assignment date) or change its stage. Restricted field(s): %s",
                ", ".join(sorted(set(blocked)))))

    # ------------------------------------------------------------------ #
    #  Stage chain
    # ------------------------------------------------------------------ #
    @api.depends('stage_no', 'total_stages')
    def _compute_is_final_stage(self):
        """The last stage of the task — the only one allowed to deliver."""
        for rec in self:
            rec.is_final_stage = rec.stage_no >= rec.total_stages

    @api.depends('child_ids')
    def _compute_is_current_stage(self):
        """A stage is 'current' until the next stage is handed off from it.

        The Dashboard filters on this so a task that has run through several
        stages is counted ONCE, at the stage it is actually sitting in, rather
        than once per stage record.
        """
        for rec in self:
            rec.is_current_stage = not rec.child_ids

    @api.depends('status', 'child_ids')
    def _compute_can_hand_off(self):
        """A stage that finished its pipeline but is NOT the final stage sits at
        'ready_next_stage' — that is what can be handed off. 'Deliverable' means
        the whole task is done (final stage), and 'Failed' is terminal; neither
        starts a new stage.
        """
        for rec in self:
            rec.can_hand_off = (
                rec.status == 'ready_next_stage' and not rec.child_ids)

    @api.depends('status', 'child_ids')
    def _compute_is_locked(self):
        """Frozen once the stage is finished, or once a next stage was built on it.

        Not stored, and there is no companion 'reopened' flag: action_reopen works
        by actively reverting the record OUT of its terminal status, which drops it
        out of this compute on its own.
        """
        for rec in self:
            handed_off_or_done = bool(rec.child_ids)
            rec.is_locked = bool(
                rec.status in ('deliverable', 'failed') or handed_off_or_done)
            # Same, minus the Failed state: reason/notes stay writable while failed.
            rec.is_doc_locked = bool(
                rec.status == 'deliverable' or handed_off_or_done)

    def _stage_map(self):
        """``{task_id: {stage_no: record}}`` for every task in ``self``, AS VISIBLE TO
        THE CURRENT USER, in ONE search.

        Resolved with a search rather than by walking parent_id / child_ids, and with NO
        sudo, because the record rules are what decide who sees which stage: a tasker is
        scoped to the stage assigned to them, so the sibling stage legitimately resolves
        to nothing for them, while a QL/PL gets both.

        The search matters. A search is FILTERED by the rules and simply comes back
        empty, whereas reading a field off a forbidden record reached through a relation
        RAISES AccessError — so walking parent_id would crash a stage-2 tasker's form
        instead of just hiding stage 1 from it.

        Batched on purpose. This used to be a per-record helper (``_sibling_stages``,
        with an ensure_one) called once by _compute_stage_targets and AGAIN by
        _compute_stage_mirrors, with _compute_stage_chain running a third search of its
        own — up to 4 rule-filtered searches per record, each re-evaluating the rule
        domain. All three computes now share this one query.
        """
        task_ids = [t for t in set(self.mapped('task_id')) if t]
        if not task_ids:
            return {}
        stages = self.env['project.tracker.allocation'].search(
            [('task_id', 'in', task_ids)], order='stage_no asc')
        by_task = {}
        for stage in stages:
            by_task.setdefault(stage.task_id, {})[stage.stage_no] = stage
        return by_task

    @staticmethod
    def _siblings_from(stages, rec):
        """Pull (stage1, stage2) for ``rec`` out of a ``_stage_map`` bucket.

        ``rec`` itself always stands in for its own stage: it is trivially visible to
        the user reading it, even when the search that built the map did not surface it
        (a fresh, unsaved record has no row yet).
        """
        empty = rec.browse()
        stage1 = stages.get(1, empty)
        stage2 = stages.get(2, empty)
        if rec.stage_no and rec.stage_no >= 2:
            stage2 = rec
        else:
            stage1 = rec
        return stage1, stage2

    @api.depends('task_id', 'stage_no', 'parent_id', 'child_ids')
    def _compute_stage_chain(self):
        """The stages of this task the current user may see, oldest first, and who works
        each one. Rule-scoped: a tasker sees only their own stage, a QL/PL the chain."""
        by_task = self._stage_map()
        for rec in self:
            stages = by_task.get(rec.task_id) or {}
            if not rec.task_id:
                rec.stage_ids = False
                rec.stage1_tasker_id = False
                rec.stage2_tasker_id = False
                continue
            visible = rec.browse()
            for _no, stage in sorted(stages.items()):
                visible |= stage
            rec.stage_ids = visible
            rec.stage1_tasker_id = (
                stages[1].tasker_member_id.id if 1 in stages else False)
            rec.stage2_tasker_id = (
                stages[2].tasker_member_id.id if 2 in stages else False)

    @api.depends('task_id', 'stage_no', 'parent_id', 'child_ids')
    def _compute_stage_targets(self):
        by_task = self._stage_map()
        for rec in self:
            _stage1, stage2 = self._siblings_from(by_task.get(rec.task_id) or {}, rec)
            # The form gates the Stage 2 tab on this bare boolean rather than on a
            # Many2one to the stage-2 record: a boolean cannot blow up on a
            # display_name fetch.
            rec.has_stage_2 = bool(stage2)

    @api.depends(
        'task_id', 'stage_no', 'parent_id', 'child_ids',
        *_STAGE1_MIRROR,
        *['parent_id.%s' % name for name in _STAGE1_MIRROR],
        *['child_ids.%s' % name for name in _STAGE2_MIRROR],
    )
    def _compute_stage_mirrors(self):
        """Project both stages of the task onto this record so one form can show them.

        Scoped by the record rules (see _stage_map), so a tasker only ever sees the
        stage assigned to them and the other stage's mirrors come back blank. The mirrors
        are read-only — no inverse on purpose. Each stage is still EDITED on its own
        record; making these writable would fire a cross-record write on every save,
        which a tasker has no access to.
        """
        by_task = self._stage_map()
        for rec in self:
            stage1, stage2 = self._siblings_from(by_task.get(rec.task_id) or {}, rec)
            for name in _STAGE1_MIRROR:
                rec['s1_%s' % name] = _mirror_value(stage1, name)
            for name in _STAGE2_MIRROR:
                rec['s2_%s' % name] = _mirror_value(stage2, name)

    def action_hand_off(self):
        """Open the wizard that allocates the NEXT stage of this task."""
        self.ensure_one()
        if not self.can_hand_off:
            raise ValidationError(_(
                "Only a completed (Deliverable) stage that has not been handed "
                "off yet can start the next stage."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Hand off to Stage %s', self.stage_no + 1),
            'res_model': 'project.tracker.stage.handoff',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_allocation_id': self.id},
        }

    def action_view_stage_chain(self):
        """Show every stage of this task (the whole chain), oldest first."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Task %s — all stages', self.task_id),
            'res_model': 'project.tracker.allocation',
            'domain': [('task_id', '=', self.task_id)],
            'view_mode': 'list,form',
            'context': {'default_task_id': self.task_id},
        }

    def action_manual_save(self):
        """Explicit Save button at the bottom of the form.

        Clicking an object button on a dirty record makes Odoo SAVE it first (and run
        the same validity checks as auto-save — missing required fields block the save
        and are highlighted), so this only needs to confirm the save happened. It is a
        thin convenience next to auto-save, not a different save path.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Saved"),
                'message': _("Your changes have been saved."),
                'type': 'success',
                'sticky': False,
            },
        }

    # Every field a stage's own worker fills in. A locked record accepts writes to
    # none of them. Deliberately does NOT include the computed status fields or
    # date_final: _stamp_completion writes date_final on every save, and status /
    # final_status / function are recomputed by the ORM, so freezing them here
    # would deadlock the record's own bookkeeping.
    _LOCKED_INPUT_FIELDS = (
        'drive_link', 'tasker_qc_notes',
        'pl_verified_status', 'pl_verified_reason', 'pl_verified_notes',
        'baseline_ready_status', 'baseline_ready_reason', 'baseline_ready_notes',
        'baseline_drive_link',
        'baseline_gen_status', 'baseline_gen_reason', 'baseline_gen_notes',
        'rubric_score', 'pytest_score', 'overall_score', 'qced_by',
        'manual_qc_status', 'manual_qc_reason', 'manual_qc_notes',
    )

    # The subset of the above that stays editable WHILE FAILED (softer is_doc_locked
    # rule: frozen only on Deliverable or hand-off). Two kinds:
    #   * the reason/notes, so a failure can be documented after it happens;
    #   * the gate STATUS fields, so whoever marked a gate Failed can flip it back —
    #     un-failing is self-service, no privileged Reopen needed. Marking a gate
    #     Failed used to be a one-way door because the field that failed the task was
    #     itself frozen the instant it changed.
    # Must stay a subset of _LOCKED_INPUT_FIELDS, or the force-save guarantee (tested)
    # would miss them.
    _FAILED_EDITABLE_FIELDS = frozenset((
        'pl_verified_status', 'pl_verified_reason', 'pl_verified_notes',
        'baseline_ready_status', 'baseline_ready_reason', 'baseline_ready_notes',
        'baseline_gen_status', 'baseline_gen_reason', 'baseline_gen_notes',
        'manual_qc_status', 'manual_qc_reason', 'manual_qc_notes',
    ))

    def _check_locked(self, vals):
        """Refuse stage-input edits on a finished or handed-off record.

        The view's ``readonly="is_locked"`` is only a hint -- it is trivially
        bypassed over RPC -- so the freeze has to be enforced here, where every
        write goes through.

        The ``project_tracker_reopen`` escape hatch is gated on the QL/admin groups. It used
        to be honoured on the context key ALONE, which made it useless as a control:
        context travels with every RPC call and is fully attacker-supplied, so any
        tasker could write with ``context={'project_tracker_reopen': True}`` and edit a frozen
        Deliverable record -- overwriting the scores and QC sign-offs that the next
        stage and the Daily Tracker were derived from. A context flag is a statement
        of intent, never a proof of privilege: re-check the group.
        """
        if self.env.su:
            return
        if self.env.context.get('project_tracker_reopen') and self._is_tracker_manager():
            return
        touched = [f for f in self._LOCKED_INPUT_FIELDS if f in vals]
        if not touched:
            return
        for rec in self:
            for f in touched:
                # Fields that stay live while Failed (reason/notes to document it, and
                # the gate status to un-fail it) obey the softer lock. Every other
                # input obeys the full lock.
                locked = (rec.is_doc_locked if f in self._FAILED_EDITABLE_FIELDS
                          else rec.is_locked)
                if not locked:
                    continue
                if rec.child_ids:
                    raise UserError(_(
                        "Task %s stage %s has already been handed off to the next "
                        "stage, which was built on this data. Delete the next stage "
                        "before changing it.", rec.task_id, rec.stage_no))
                raise UserError(_(
                    "Task %s stage %s is %s and its results are frozen. Use Reopen "
                    "to make changes.",
                    rec.task_id, rec.stage_no,
                    dict(self.STATUS_SELECTION).get(rec.status, rec.status)))

    def action_reopen(self):
        """Take a finished stage back out of its terminal status so it can be
        corrected, and say so in the chatter.

        Reverting the statuses IS the unlock: is_locked is derived from status, so
        once the record is no longer Deliverable/Failed it is editable again, and
        _stamp_completion clears date_final -- which withdraws the delivery credit
        the Daily Tracker had already counted. That is the point: the work is not
        finished any more.
        """
        if not self._is_tracker_manager():
            raise AccessError(_(
                "Only a Quality Lead or an administrator can reopen a finished "
                "task."))
        for rec in self:
            if not rec.is_locked:
                continue
            if rec.child_ids:
                raise UserError(_(
                    "Task %s stage %s has already been handed off. Delete stage "
                    "%s before reopening it.",
                    rec.task_id, rec.stage_no, rec.stage_no + 1))
            was = dict(self.STATUS_SELECTION).get(rec.status, rec.status)
            vals = {}
            for gate, reason in (
                    ('pl_verified_status', 'pl_verified_reason'),
                    ('baseline_ready_status', 'baseline_ready_reason'),
                    ('baseline_gen_status', 'baseline_gen_reason'),
                    ('manual_qc_status', 'manual_qc_reason')):
                if rec[gate] == 'failed':
                    vals[gate] = 'in_progress'
                    vals[reason] = False
            # A Deliverable stage is held there by its Manual QC sign-off; taking
            # that back is what returns it to the pipeline.
            if rec.manual_qc_status == 'done':
                vals['manual_qc_status'] = 'in_progress'
            if vals:
                rec.with_context(project_tracker_reopen=True).write(vals)
            rec.message_post(body=_(
                "Reopened from %(was)s by %(user)s.",
                was=was, user=self.env.user.name))
        return True

    def action_reset_stage(self):
        """Reset THIS stage only — clear its inputs back to its own starting point,
        leaving every other stage of the task untouched.

        The per-stage counterpart to action_reset_task (which resets the whole
        task). A stage that a LATER stage was built on cannot be reset in place —
        the later stage's work depends on this one's hand-off — so it is blocked
        with the same rule action_reopen uses: remove the later stage first.

        Unlike Reset Task, the record is NOT deleted, so the form stays on it.
        QL/PL-gated, confirmed in the UI, logged to the chatter.
        """
        self.ensure_one()
        if not self._is_tracker_manager():
            raise AccessError(_("Only a Project Lead / QL can reset a stage."))
        if self.child_ids:
            raise UserError(_(
                "Task %(task)s stage %(n)s was handed off to stage %(next)s, which "
                "was built on it. Reset or delete stage %(next)s first.",
                task=self.task_id, n=self.stage_no, next=self.stage_no + 1))

        was = dict(self.STATUS_SELECTION).get(self.status, self.status)
        # project_tracker_reopen: this stage may be locked (Deliverable/Failed). The guard
        # honours the flag only for a manager, which we checked above. Clearing the
        # inputs drops the status back down; _stamp_completion then withdraws any
        # delivery credit, exactly as reopen/reset_task do.
        self.with_context(project_tracker_reopen=True).write(dict(self._RESET_VALUES))
        self.message_post(body=_(
            "Stage %(n)s reset by %(user)s (was %(was)s). Its inputs were cleared; "
            "the rest of the task is unchanged.",
            n=self.stage_no, user=self.env.user.name, was=was))
        return True

    # Every stage input restored to its as-created value by action_reset_task.
    # The three-status gates go back to their column default ('in_progress'), the
    # rest are simply cleared.
    _RESET_VALUES = {
        'drive_link': False,
        'tasker_qc_notes': False,
        'pl_verified_status': 'in_progress',
        'pl_verified_reason': False,
        'pl_verified_notes': False,
        'baseline_ready_status': 'in_progress',
        'baseline_ready_reason': False,
        'baseline_ready_notes': False,
        'baseline_drive_link': False,
        'baseline_gen_status': 'in_progress',
        'baseline_gen_reason': False,
        'baseline_gen_notes': False,
        'rubric_score': 0.0,
        'pytest_score': 0.0,
        'overall_score': 0.0,
        'qced_by': False,
        'manual_qc_status': 'in_progress',
        'manual_qc_reason': False,
        'manual_qc_notes': False,
    }

    def action_reset_task(self):
        """Reset the WHOLE task — every stage — back to a clean, unstarted state.

        This is the counterpart action_reopen never was. Reopen un-freezes ONE
        stage and leaves its work in place; this throws the work away and starts
        the task over:

          * stages 2..N are DELETED (deepest first — parent_id is ondelete=restrict,
            so a stage cannot be removed while a later one still points at it),
          * stage 1 keeps its IDENTITY — task_id, persona, tasker, assigned PL/QL and
            assignment date all survive — and has every stage input wiped: drive
            links, QC gates, reasons, notes, scores and qced_by,
          * ``date_final`` clears itself, because _stamp_completion follows the
            recomputed status back down to 'in_progress'. The delivery credit the
            Daily Tracker had counted is withdrawn, which is the point: the work no
            longer exists.

        Destructive and irreversible, so it is QL/PL-gated (the same guard as
        action_reopen), confirmed in the UI, and logged to the chatter of the stage
        that survives — with the count of what was destroyed.
        """
        self.ensure_one()
        if not self._is_tracker_manager():
            raise AccessError(_(
                "Only a Project Lead / QL can reset a task."))
        if not self.task_id:
            raise UserError(_("This allocation has no Task ID to reset."))

        # sudo: a later stage can sit under a DIFFERENT lead, so the caller may not
        # be able to see it under the team-scoped record rules — but the task is one
        # unit and resetting it half-way would leave an orphaned stage 2 pointing at
        # a stage 1 that no longer has the work it was built on. The QL/PL check
        # above is the authorisation; this is just reach.
        stages = self.env['project.tracker.allocation'].sudo().search(
            [('task_id', '=', self.task_id)], order='stage_no asc')
        if not stages:
            return True

        first = stages[0]
        later = stages[1:]
        destroyed = len(later)
        if later:
            # deepest stage first: parent_id is ondelete='restrict'
            later.sorted(key=lambda s: s.stage_no, reverse=True).unlink()

        # project_tracker_reopen: stage 1 may be locked (Deliverable/Failed, or it was locked
        # by the very children we just deleted). sudo already bypasses the lock; the
        # flag keeps the intent explicit if the sudo is ever removed.
        first.with_context(project_tracker_reopen=True).write(dict(self._RESET_VALUES))

        first.message_post(body=_(
            "Task reset by %(user)s. All stage inputs were cleared%(also)s. "
            "The task starts again from Tasker QC; its tasker, persona and "
            "leads are unchanged.",
            user=self.env.user.name,
            also=(_(" and %s later stage(s) were deleted", destroyed)
                  if destroyed else ""),
        ))

        # self may BE one of the deleted stages, so always land on the survivor.
        return {
            'type': 'ir.actions.act_window',
            'name': _('Task %s', first.task_id),
            'res_model': 'project.tracker.allocation',
            'res_id': first.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # A stage's own work is finished in any of these states. Note this includes
    # 'ready_next_stage': that tasker DID complete their stage — they just are not
    # the last stage — so the completion must still be stamped, otherwise the
    # Daily Tracker (which keys off date_final) would silently drop their credit.
    _RECORD_DONE_STATES = ('deliverable', 'failed', 'ready_next_stage')

    def _stamp_completion(self):
        """Stamp the completion datetime when this stage's work is finished, and
        clear it again if it later reverts to an in-progress stage — so the Daily
        Tracker never attributes a completion to a stale date.

        Two grouped writes instead of one per record. Assigning ``rec.date_final``
        in a loop fires a fresh ``write`` per row, each re-running all three guards
        and recursing back into this method — trivial for a form save, but the bulk
        wizard creates thousands of rows in one call.
        """
        to_stamp = self.browse()
        to_clear = self.browse()
        for rec in self:
            if rec.status in self._RECORD_DONE_STATES:
                if not rec.date_final:
                    to_stamp |= rec
            elif rec.date_final:
                to_clear |= rec
        # sudo + project_tracker_reopen: date_final is bookkeeping the record owes itself, not
        # a user edit. It must land even on a record that is (now) locked, and
        # regardless of who triggered the status change.
        if to_stamp:
            to_stamp.sudo().with_context(project_tracker_reopen=True).write(
                {'date_final': fields.Datetime.now()})
        if to_clear:
            to_clear.sudo().with_context(project_tracker_reopen=True).write(
                {'date_final': False})

    @api.model
    def _normalise_task_id(self, vals):
        """Store the Task ID in canonical lowercase.

        The UUID regex is case-insensitive, so without this
        'ABCDEF01-...' and 'abcdef01-...' are the SAME uuid but two DIFFERENT
        strings — they would chain into two separate tasks and slip straight past
        the unique(task_id, stage_no) key. Normalising on the way in means one uuid
        is always exactly one task.
        """
        tid = vals.get('task_id')
        if tid and isinstance(tid, str):
            vals['task_id'] = tid.strip().lower()

    def write(self, vals):
        self._normalise_task_id(vals)
        # _sync_tasker FIRST, then guard. It resolves tasker_email -> tasker_member_id,
        # so guarding before it meant the guard inspected a vals dict that did not yet
        # contain the privileged field the write was about to set — a tasker could
        # re-assign their task by writing an email. Guard the resolved vals.
        self._sync_tasker([vals])
        self._guard_privileged_fields(vals)
        self._check_locked(vals)
        # `status` is computed from the fields in `vals`, so capture the OLD value
        # before the write to detect a transition afterwards.
        old_status = {rec.id: rec.status for rec in self}
        res = super().write(vals)
        # status is computed; stamp completion based on the (re)computed value.
        self._stamp_completion()
        self._stamp_status_since(old_status)
        return res

    def _stamp_status_since(self, old_status_by_id):
        """Reset the in-status clock whenever the step changed. sudo + super so it
        lands as bookkeeping even on a record that just became locked, and never
        re-enters this override."""
        changed = self.filtered(
            lambda r: r.status != old_status_by_id.get(r.id))
        if changed:
            super(Kensei2TrackerAllocation, changed.sudo()).write(
                {'status_since': fields.Datetime.now()})

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        for vals in vals_list:
            self._normalise_task_id(vals)
            # A new record's status is computed on create; start its clock now.
            vals.setdefault('status_since', now)
        self._sync_tasker(vals_list)
        for vals in vals_list:
            self._guard_privileged_fields(vals)
        records = super().create(vals_list)
        records._stamp_completion()
        return records
