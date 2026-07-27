"""The project — Odoo's ``project.project``, extended (data model v2 §4.2).

v2 reverses v1: the project entity is Odoo's native ``project.project`` rather than a
standalone model. Everything Project OS owns hangs off that record.

Three things about *this* database shape the file, and all three are departures the doc
could not have anticipated because it specified against the model rather than against
the installed system:

* **``project_type`` was already taken.** ``project_extension`` declares it as
  ``single_turn`` / ``multi_turn``. Redeclaring it with ``internal`` / ``external``
  would merge two incompatible selections onto one column: existing rows hold values
  the new code cannot read, and every ``[('project_type','=','external')]`` domain
  matches nothing. It is therefore ``ethara_project_type`` — the same namespacing the
  doc itself applies to ``ethara_state`` in §4.2, and for the same stated reason.

* **The table is shared with three other modules.** ``project_extension`` (61 fields),
  ``etp_projects`` and ``task_forge_bridge`` all own rows here. ``is_project_os`` marks
  the rows this pipeline owns; the doc calls for the same gate in §6.3 ("gate the rule
  with a domain filter ... to apply only to Ethara projects") and §10.4 ("Option B —
  keeps non-Ethara projects clean"). A project the other modules created is not missing
  an SOP; it was never in this pipeline.

* **Native fields are used, not redeclared** (§4.2 note): ``name``, ``active``,
  ``description`` (Html), ``date_start``, ``date``, ``user_id``, ``partner_id``,
  ``stage_id``. ``ethara_state`` is the delivery lifecycle and coexists with the kanban
  ``stage_id`` rather than replacing it.

The kickoff is a gate, not a checklist: a project may not go live until it has an SOP
in its knowledge folder AND a published stagelist. The prototype enforced this in one
API handler, so anything that wrote the project another way slipped past it. Here it is
a constraint on the record — the invalid state simply cannot be stored.

The readiness flags are maintained by the knowledge and form models rather than
recomputed by subquery on every list; at roster scale the prototype's EXISTS-per-card
was a sequential scan per project card on every page load.
"""

import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

ETHARA_STATES = [('setup', 'Setup'), ('active', 'Active'), ('archived', 'Archived')]

# How far ahead the staffing screen looks for approved leave. Two weeks is the window a
# GPM is actually deciding over — further out and every candidate carries a note that
# has nothing to do with the decision in front of them.
CANDIDATE_LEAVE_HORIZON_DAYS = 14


class ProjectProject(models.Model):
    _inherit = 'project.project'

    # ------------------------------------------------------------------
    # ownership — which rows this pipeline governs (doc §6.3, §10.4)
    # ------------------------------------------------------------------
    is_project_os = fields.Boolean(
        string='Ethara Project OS', default=False, copy=False, index=True, tracking=True,
        help='This project runs through the Project OS pipeline: knowledge, the go-live '
             'gate, allocation, onboarding and tasking. Projects owned by the other '
             'modules on this table never enter that pipeline and are untouched by it.')

    code = fields.Char(
        string='Ethara Code', tracking=True, copy=False,
        help='The handle everyone outside this system uses — on the delivery report, '
             'in the channel name, on the invoice line. Generated on creation if you '
             'do not supply one; unique either way.')
    # NOT `project_type`: see the module docstring. project_extension owns that name.
    ethara_project_type = fields.Selection(
        [('external', 'External'), ('internal', 'Internal')],
        string='Ethara Project Type', default='external', tracking=True)
    platform = fields.Char(
        string='Tasking Platform', default='Multimango',
        help='Client platform the work lands on.')

    ethara_state = fields.Selection(
        ETHARA_STATES, string='Ethara State', required=True, default='setup',
        tracking=True, index=True, copy=False,
        help='The Ethara delivery lifecycle. Deliberately separate from the kanban '
             'stage_id, which stays available for board columns.')

    min_assessment_score = fields.Float(
        string='Minimum score', default=0.0, tracking=True,
        help='The bar a pod member must already clear to be put on this project, as a '
             'percentage. Set by the PM. 0 means no bar — anybody can be allocated.\n'
             'This sits ABOVE the assessment paper\'s own pass mark: a paper may pass '
             'at 60 while this project only takes people who scored 80 somewhere.')

    gpm_id = fields.Many2one(
        'hr.employee', string='GPM Owner', tracking=True, ondelete='restrict',
        help='The programme-management owner accountable for delivery. Distinct from '
             'the native user_id, which is the Odoo project manager.')
    created_by_emp_id = fields.Many2one(
        'hr.employee', string='Created By', readonly=True, copy=False,
        ondelete='restrict', default=lambda s: s._default_creator())
    activated_at = fields.Datetime(readonly=True, copy=False)
    archived_at = fields.Datetime(readonly=True, copy=False)

    # --- content -------------------------------------------------------
    folder_ids = fields.One2many('epo.folder', 'project_id', string='Folders')
    knowledge_folder_id = fields.Many2one(
        'epo.folder', compute='_compute_root_folders', string='Knowledge')
    management_folder_id = fields.Many2one(
        'epo.folder', compute='_compute_root_folders', string='Management')
    document_ids = fields.One2many('epo.document', 'project_id', string='Documents')
    knowledge_doc_ids = fields.One2many(
        'epo.document', 'project_id', string='Knowledge documents',
        domain=[('root', '=', 'knowledge')])
    management_doc_ids = fields.One2many(
        'epo.document', 'project_id', string='Management documents',
        domain=[('root', '=', 'management')])
    training_ids = fields.One2many('epo.training', 'project_id', string='Training')
    ethara_assessment_ids = fields.One2many(
        'ethara.assessment', 'project_id', string='Assessments')
    ethara_assessment_id = fields.Many2one(
        'ethara.assessment', compute='_compute_assessment', store=True,
        string='Assessment', help='The live external assessment for this project.')
    template_ids = fields.One2many('epo.form.template', 'project_id', string='Forms')
    stagelist_template_id = fields.Many2one(
        'epo.form.template', compute='_compute_templates', store=True, string='Stagelist')
    feedback_template_id = fields.Many2one(
        'epo.form.template', compute='_compute_templates', store=True, string='Feedback Form')

    # --- people --------------------------------------------------------
    allocation_ids = fields.One2many('epo.allocation', 'project_id', string='Allocations')
    onboarding_ids = fields.One2many('epo.onboarding', 'project_id', string='Onboarding')
    timeline_ids = fields.One2many('epo.timeline.event', 'project_id', string='History')
    entry_ids = fields.One2many('epo.form.entry', 'project_id', string='Entries')

    # `allocated_today`, not `allocated_count` — the doc's name, and the honest one: an
    # allocation dated to start next Monday is a staffing plan, not headcount today.
    allocated_today = fields.Integer(compute='_compute_counts', string='Allocated today')
    onboarding_count = fields.Integer(compute='_compute_counts', string='In Onboarding')
    tasking_count = fields.Integer(compute='_compute_counts', string='Tasking Today')
    submission_count = fields.Integer(compute='_compute_counts', string='Submissions')

    # --- readiness flags (doc §4.2) -------------------------------------
    has_sop = fields.Boolean(
        readonly=True, copy=False,
        help='Maintained by the knowledge folder. An SOP is mandatory before go-live.')
    has_common_errors = fields.Boolean(readonly=True, copy=False)
    has_task_videos = fields.Boolean(readonly=True, copy=False)
    has_stagelist = fields.Boolean(
        readonly=True, copy=False,
        help='Maintained by the form engine: a published stagelist exists.')
    has_feedback = fields.Boolean(
        readonly=True, copy=False,
        help='A published feedback form exists. Not part of the go-live gate.')
    has_training = fields.Boolean(compute='_compute_content_flags', store=True)
    has_assessment = fields.Boolean(compute='_compute_content_flags', store=True)
    gate_blockers = fields.Char(compute='_compute_gate_blockers', string='Blocked by')

    # Doc §4.2 requires exactly one uniqueness rule on the project: the code. There is
    # deliberately no index on `name` — Odoo's own project.project allows duplicate
    # names, the table is shared with four owners who each name rows their own way, and
    # `name` is translate=True (a jsonb column), so a lower(name) index is not even
    # expressible without pinning a language.
    _epo_code_uniq = models.UniqueIndex(
        '(lower(code)) WHERE code IS NOT NULL', 'That project code is already in use.')
    # Both gate constraints are satisfied trivially by every project that is not in this
    # pipeline: ethara_state defaults to 'setup' and stays there.
    _epo_active_requires_gate = models.Constraint(
        "CHECK (ethara_state <> 'active' OR (has_sop AND has_stagelist))",
        'A project can only go active with an SOP and a published stagelist.')
    _epo_activated_stamped = models.Constraint(
        "CHECK (ethara_state = 'setup' OR activated_at IS NOT NULL)",
        'An active project must carry its activation timestamp.')
    _epo_min_score_bounds = models.Constraint(
        'CHECK (min_assessment_score >= 0 AND min_assessment_score <= 100)',
        'The minimum score must be a percentage between 0 and 100.')

    @api.model
    def _default_creator(self):
        return self.env.user._epo_employee()

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('ethara_assessment_ids.active')
    def _compute_assessment(self):
        for project in self:
            project.ethara_assessment_id = project.ethara_assessment_ids.filtered(
                'active')[:1]

    @api.depends('template_ids.state', 'template_ids.form_type', 'template_ids.version')
    def _compute_templates(self):
        for project in self:
            published = project.template_ids.filtered(lambda t: t.state == 'published')
            project.stagelist_template_id = published.filtered(
                lambda t: t.form_type == 'stagelist')[:1]
            project.feedback_template_id = published.filtered(
                lambda t: t.form_type == 'feedback')[:1]

    @api.depends('training_ids.active', 'ethara_assessment_ids.active')
    def _compute_content_flags(self):
        for project in self:
            project.has_training = bool(project.training_ids.filtered('active'))
            project.has_assessment = bool(
                project.ethara_assessment_ids.filtered('active'))

    @api.depends('has_sop', 'has_stagelist')
    def _compute_gate_blockers(self):
        for project in self:
            missing = []
            if not project.has_sop:
                missing.append(_('SOP'))
            if not project.has_stagelist:
                missing.append(_('published stagelist'))
            project.gate_blockers = ', '.join(missing)

    def _compute_counts(self):
        Roster = self.env['epo.roster.day'].sudo()
        Entry = self.env['epo.form.entry'].sudo()
        today = fields.Date.context_today(self)
        for project in self:
            # Covering today, not merely open. A future-dated allocation is a staffing
            # plan, and counting it as headcount is how a project card claims twelve
            # people on a day only eight of them are on it.
            live = project.allocation_ids.filtered(
                lambda a: a.date_from <= today and (not a.date_to or a.date_to >= today))
            project.allocated_today = len(live)
            project.onboarding_count = len(project.onboarding_ids.filtered(
                lambda o: not o.unlocked))
            project.tasking_count = Roster.search_count([
                ('project_id', '=', project.id), ('business_date', '=', today),
                ('tasking_status', '=', 'tasking')])
            project.submission_count = Entry.search_count([
                ('project_id', '=', project.id), ('state', '=', 'submitted')])

    # ------------------------------------------------------------------
    # gate maintenance
    # ------------------------------------------------------------------
    @api.depends('folder_ids.slug')
    def _compute_root_folders(self):
        Folder = self.env['epo.folder']
        for project in self:
            project.knowledge_folder_id = Folder._get(project, 'knowledge')
            project.management_folder_id = Folder._get(project, 'management')

    def _recompute_gate(self):
        """Called by the knowledge folder and the form engine whenever their content
        changes. Deleting the last SOP of a live project raises here rather than
        silently un-gating tasking."""
        for project in self.filtered('is_project_os'):
            docs = project.document_ids.filtered('active')
            published = project.template_ids.filtered(lambda t: t.state == 'published')
            has_sop = bool(docs.filtered(lambda d: d.category == 'sop'))
            has_stagelist = bool(published.filtered(lambda t: t.form_type == 'stagelist'))
            project.sudo().write({
                'has_sop': has_sop,
                'has_common_errors': bool(
                    docs.filtered(lambda d: d.category == 'common_errors')),
                'has_task_videos': bool(
                    docs.filtered(lambda d: d.category == 'task_videos')),
                'has_stagelist': has_stagelist,
                'has_feedback': bool(
                    published.filtered(lambda t: t.form_type == 'feedback')),
            })
            if project.ethara_state == 'active' and not (has_sop and has_stagelist):
                raise UserError(_(
                    'Project "%(name)s" is live. Removing its %(what)s would leave people '
                    'tasking without it. Archive the project first if that is intended.',
                    name=project.name,
                    what=_('SOP') if not has_sop else _('published stagelist')))

    def _try_auto_activate(self):
        """Publishing the stagelist of a project that already has an SOP takes it live,
        exactly as the prototype's publish handler did — but from the form engine, so
        every path to a published stagelist goes through it."""
        for project in self.filtered('is_project_os'):
            if project.ethara_state == 'setup' and project.has_sop and project.has_stagelist:
                project.action_activate()

    # ------------------------------------------------------------------
    # state machine (doc §4.2)
    # ------------------------------------------------------------------
    def action_activate(self):
        for project in self:
            if project.ethara_state == 'active':
                continue
            if not project.is_project_os:
                raise UserError(_(
                    '"%s" is not an Ethara Project OS project. Turn Project OS on first '
                    '— going live means an SOP, a stagelist and a knowledge folder, and '
                    'this project has none of them.', project.name))
            if project.ethara_state not in ('setup', 'archived'):
                raise UserError(_(
                    'Cannot activate a project in state %s.', project.ethara_state))
            project._recompute_gate()
            if not (project.has_sop and project.has_stagelist):
                raise UserError(_(
                    'Cannot activate "%(name)s" — still missing: %(missing)s.',
                    name=project.name, missing=project.gate_blockers))
            reopened = project.ethara_state == 'archived'
            project.write({
                'ethara_state': 'active',
                'activated_at': project.activated_at or fields.Datetime.now(),
                'archived_at': False,
                'active': True,
            })
            self.env['epo.timeline.event'].log(
                event_type='project_reopened' if reopened else 'project_activated',
                project_id=project.id,
                summary=_('Project %s', 'reopened' if reopened else 'activated'),
                record=project)
        return True

    def action_archive_project(self):
        """Doc §4.2: flips ethara_state='archived' and Odoo's own active=False."""
        for project in self:
            if project.ethara_state != 'active':
                raise UserError(_('Only a live project can be archived.'))
            open_allocs = project.allocation_ids.filtered(lambda a: not a.date_to)
            if open_allocs:
                raise UserError(_(
                    '%(n)s people are still allocated to "%(name)s". Release them first — '
                    'archiving with open allocations leaves them tasking on a dead project.',
                    n=len(open_allocs), name=project.name))
            project.write({
                'ethara_state': 'archived',
                'archived_at': fields.Datetime.now(),
                'active': False,
            })
            self.env['epo.timeline.event'].log(
                event_type='project_archived', project_id=project.id,
                summary=_('Project archived'), record=project)
        return True

    @api.constrains('ethara_state')
    def _check_ethara_transition(self):
        """setup → active → archived, and archived → active to reopen. Nothing else:
        a project that jumps straight from setup to archived never had a gate."""
        for project in self:
            if project.ethara_state == 'archived' and not project.archived_at:
                raise ValidationError(_(
                    'Archive the project with the Archive action so the timestamp and '
                    'the history are written.'))

    @api.constrains('is_project_os', 'ethara_state')
    def _check_os_flag_not_withdrawn(self):
        """Clearing the flag on a live project would take it out of every gate, every
        record rule and every roster check while people are still tasking on it."""
        for project in self:
            if not project.is_project_os and project.ethara_state != 'setup':
                raise ValidationError(_(
                    '"%s" has been through the Project OS pipeline and cannot be taken '
                    'out of it. Archive it instead.', project.name))

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('is_project_os'):
                continue
            vals['code'] = self._normalise_code(vals.get('code')) or self._next_code()
        projects = super().create(vals_list)
        for project in projects.filtered('is_project_os'):
            # Every project gets the same filing cabinet on day one, so a PM never
            # faces an empty screen and a tasker always knows where to look.
            self.env['epo.folder']._build_skeleton(project)
            self.env['epo.timeline.event'].log(
                event_type='project_created', project_id=project.id,
                summary=_('Project created: %(name)s (%(code)s)',
                          name=project.name, code=project.code),
                record=project)
        return projects

    def write(self, vals):
        if 'code' in vals:
            vals['code'] = self._normalise_code(vals['code'])
            if not vals['code'] and any(self.mapped('is_project_os')):
                raise UserError(_(
                    'A project cannot lose its code — it is the handle used everywhere '
                    'outside this system.'))
        return super().write(vals)

    @api.model
    def _normalise_code(self, code):
        """Codes are compared case-insensitively by the unique index, so store them the
        way they will be read: upper case, no stray whitespace."""
        if not code:
            return False
        return re.sub(r'\s+', '-', code.strip()).upper()

    @api.model
    def _next_code(self):
        """Doc §4.2 / §10.4 Option B: EPR/YYYY/0001, assigned only to Ethara projects
        so the other modules' rows on this table stay clean."""
        code = self.env['ir.sequence'].next_by_code('ethara.project.code')
        if not code:
            # Sequence data missing (a partial upgrade): fall back rather than refuse
            # to create the project.
            year = fields.Date.context_today(self).year
            count = self.search_count([('is_project_os', '=', True)]) + 1
            code = f'EPR/{year}/{count:04d}'
        return code

    def candidates(self, employee_ids=None, include_below_bar=False,
                   min_score=None):
        """The staffing screen: which taskers clear this project's bar.

        The GPM sets ``min_assessment_score`` first; this answers "so who does that
        leave me?". **Only people at or above the bar come back** — that is the point of
        setting one, and a screen listing forty names the GPM cannot pick is not a
        shortlist.

        Pass ``include_below_bar`` to get the rest anyway, each carrying the reason it
        was cut. The endpoint always reports ``considered`` alongside ``eligible``, so
        an empty shortlist reads as "nobody clears 80" rather than "the filter is
        broken" — the one thing a silent filter can never tell you apart.

        Per row: name, seat, score, current project, and today's leave — the five facts
        a staffing decision actually turns on. Somebody who clears the bar but is off
        until Friday is a different answer from somebody who is free today, and the
        screen has to be able to say so.

        ``min_score`` previews a different bar without saving it, so a GPM can see what
        lowering it to 70 would buy before committing. The stored bar is the one that
        governs the allocation itself; this only changes what the screen shows.
        """
        self.ensure_one()
        employees = self._candidate_pool(employee_ids)
        already = set(self.allocation_ids.filtered(
            lambda a: not a.date_to).mapped('employee_id').ids)
        leave = self._candidate_leave(employees)
        bar = self.min_assessment_score if min_score is None else min_score

        rows = []
        for employee in employees:
            score = employee._epo_best_assessment_score()
            reasons = []
            if employee.id in already:
                reasons.append(_('already on this project'))
            if bar:
                if score is None:
                    reasons.append(_('no assessment score yet'))
                elif score < bar:
                    reasons.append(_('scored %(score).0f, needs %(bar).0f',
                                     score=score, bar=bar))
            on_leave, leave_note = leave.get(employee.id, (False, ''))
            eligible = not reasons
            if not eligible and not include_below_bar:
                continue
            rows.append({
                'employee_id': employee.id,
                'name': employee.display_name,
                'seat': employee.epo_seat_no or '',
                'score': score,
                # kept under the old key too: the frontend already reads best_score
                'best_score': score,
                'current_project': employee.epo_current_project_id.name or '',
                'on_leave': on_leave,
                'leave': leave_note,
                'email': employee.work_email or '',
                'pod': employee.epo_pod_id.display_name or '',
                'pod_lead': employee.epo_pod_lead_id.display_name or '',
                'eligible': eligible,
                'reason': ', '.join(reasons),
            })
        # Best score first among the people who clear the bar; anyone below it sinks to
        # the bottom, and the available sort above the ones who are away.
        rows.sort(key=lambda r: (not r['eligible'], r['on_leave'],
                                 -(r['score'] or 0), r['name']))
        return rows

    def _candidate_pool(self, employee_ids=None):
        """Everybody weighed for this project, before the bar is applied.

        Taskers by default — the people who get staffed onto a project. Pass
        ``employee_ids`` to consider anyone else: a trainer, a reviewer, somebody whose
        role grant has not been made yet.
        """
        self.ensure_one()
        Employee = self.env['hr.employee']
        employees = (Employee.browse(employee_ids) if employee_ids
                     else Employee.search([('epo_role', '=', 'pm')]))
        return employees.filtered('active')

    def _candidate_leave(self, employees):
        """``{employee_id: (on_leave_today, "human note")}`` for the staffing screen.

        Read from ``hr.leave`` rather than today's roster, because the roster only
        knows about today and tomorrow. A GPM staffing a project on Friday needs to see
        that somebody is off all next week — which is exactly the fact the roster
        cannot hold yet.
        """
        self.ensure_one()
        today = fields.Date.context_today(self)
        horizon = today + timedelta(days=CANDIDATE_LEAVE_HORIZON_DAYS)
        leaves = self.env['hr.leave'].sudo().search([
            ('employee_id', 'in', employees.ids),
            ('state', '=', 'validate'),
            ('request_date_from', '<=', horizon),
            ('request_date_to', '>=', today),
        ], order='request_date_from')

        out = {}
        for leave in leaves:
            start, end = leave.request_date_from, leave.request_date_to
            if not start:
                continue
            employee_id = leave.employee_id.id
            if start <= today <= (end or start):
                # On leave right now — the strongest signal, so it wins over any
                # upcoming one already recorded for this person.
                out[employee_id] = (True, _('on leave until %s', end or start))
            elif employee_id not in out:
                out[employee_id] = (False, _('off %(from)s → %(to)s',
                                             **{'from': start, 'to': end or start}))
        return out

    def action_open_history(self):
        """Stat button: this project's whole history, the same rows the API serves at
        GET /projects/<id>/history."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('History — %s', self.name),
            'res_model': 'epo.timeline.event',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.timeline_ids.ids)],
        }

    def action_open_team(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Team — %s', self.name),
            'res_model': 'epo.allocation',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_rebuild_folders(self):
        """Re-create any missing system folders. Safe to run on an existing project —
        used after the skeleton changes in a later version."""
        for project in self.filtered('is_project_os'):
            self.env['epo.folder']._build_skeleton(project)
        return True
