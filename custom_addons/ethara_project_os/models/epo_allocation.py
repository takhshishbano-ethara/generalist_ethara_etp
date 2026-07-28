"""Who is on which project, from when to when — and what they were doing inside it.

Two models, because they answer two different questions and merging them loses one:

* ``epo.allocation`` — the *membership*: this person is on this project from this date
  to that date, at this percentage. It survives the project, the pod and the person's
  departure, because a delivery report a year from now still has to say who worked on
  what.
* ``epo.allocation.phase`` — the *inside* of that membership: contiguous stretches of
  onboarding, SOP reading, training, assessment, ramp-up, tasking and ramp-down, each
  with a start, an end and a day count.

The phase log is what makes "how much of this project was spent getting people ready
versus doing the work?" a query instead of an argument. It is maintained automatically
from the daily roster — nobody types it in twice.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

PHASES = [
    ('onboarding', 'Onboarding'),
    ('sop', 'SOP reading'),
    ('training', 'Training'),
    ('assessment', 'Assessment'),
    ('ramp_up', 'Ramp-up'),
    ('tasking', 'Tasking'),
    ('ramp_down', 'Ramp-down'),
]


class EpoAllocation(models.Model):
    _name = 'epo.allocation'
    _description = 'Project OS Allocation'
    _order = 'date_from desc, id desc'
    _inherit = ['mail.thread']
    _rec_name = 'display_name'

    project_id = fields.Many2one(
        'project.project', required=True, ondelete='restrict', index=True, tracking=True,
        domain=[('is_project_os', '=', True)])
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True)
    pod_id = fields.Many2one(
        'epo.pod', related='employee_id.epo_pod_id', store=True, index=True)
    role_on_project = fields.Selection(
        [('tasker', 'Tasker'), ('pl', 'Pod Lead'), ('pm', 'PM'),
         ('trainer', 'Trainer'), ('reviewer', 'Reviewer')],
        default='tasker', required=True,
        help='What they do on THIS project — independent of their org role.')

    date_from = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    date_to = fields.Date(tracking=True, help='Empty means still on the project.')
    is_open = fields.Boolean(compute='_compute_is_open', store=True, index=True)
    allocation_pct = fields.Float(
        default=100.0, required=True, string='Allocation %',
        help='Part-time membership across two projects is legitimate; 130% is not.')

    min_score_applied = fields.Float(
        readonly=True, string='Minimum score applied',
        help='The project\'s bar at the moment this person was put on it. Snapshotted '
             'so raising the bar later never retroactively disqualifies somebody who '
             'is already doing the work.')
    score_at_allocation = fields.Float(
        readonly=True, string='Score on joining',
        help='Their best graded score when they joined, for the same reason.')
    override_reason = fields.Text(
        readonly=True,
        help='Set when a PM allocated somebody below the bar anyway. Mandatory in '
             'that case, and written to the audit log.')

    allocated_by_id = fields.Many2one('res.users', ondelete='restrict', readonly=True,
                                      default=lambda s: s.env.user)
    released_by_id = fields.Many2one('res.users', ondelete='restrict', readonly=True)
    source = fields.Selection(
        [('manual', 'Manual'), ('bulk', 'Bulk'), ('import', 'Import'),
         ('migration', 'Migration')],
        required=True, default='manual')
    note = fields.Text()

    phase_ids = fields.One2many('epo.allocation.phase', 'allocation_id', string='Phases')
    onboarding_id = fields.Many2one(
        'epo.onboarding', compute='_compute_onboarding', string='Onboarding')
    current_phase = fields.Selection(
        PHASES, compute='_compute_current_phase', store=True, index=True,
        string='Current phase',
        help='The phase the open phase segment is in. Stored so a PM can filter and '
             'group by "who is still ramping up".')

    # --- the time answer ----------------------------------------------
    days_total = fields.Integer(compute='_compute_phase_stats', string='Days on project')
    days_onboarding = fields.Integer(compute='_compute_phase_stats', string='Days onboarding')
    days_training = fields.Integer(compute='_compute_phase_stats', string='Days training')
    days_assessment = fields.Integer(compute='_compute_phase_stats', string='Days assessment')
    days_tasking = fields.Integer(compute='_compute_phase_stats', string='Days tasking')
    days_to_productive = fields.Integer(
        compute='_compute_phase_stats', string='Days to productive',
        help='Calendar days from joining the project to the first day of tasking. The '
             'number a PM actually plans with.')
    submission_count = fields.Integer(compute='_compute_phase_stats', string='Submissions')

    _range_sane = models.Constraint(
        'CHECK (date_to IS NULL OR date_to >= date_from)',
        'An allocation cannot end before it starts.')
    _pct_bounds = models.Constraint(
        'CHECK (allocation_pct > 0 AND allocation_pct <= 100)',
        'Allocation percentage must be above 0 and at most 100.')
    _release_audited = models.Constraint(
        'CHECK (date_to IS NULL OR released_by_id IS NOT NULL)',
        'A closed allocation must record who released the person.')
    # The classic double-count in every headcount report. A Python check reads, decides,
    # then writes — two concurrent allocations both pass it. This does not.
    _no_self_overlap = models.Constraint(
        "EXCLUDE USING gist ("
        "employee_id WITH =, project_id WITH =, "
        "daterange(date_from, date_to, '[]') WITH &&)",
        'That person is already allocated to this project over an overlapping period.')

    @api.depends('employee_id', 'project_id', 'date_from')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s → %s' % (
                rec.employee_id.display_name or '?', rec.project_id.name or '?')

    @api.depends('date_to')
    def _compute_is_open(self):
        for rec in self:
            rec.is_open = not rec.date_to

    def _compute_onboarding(self):
        Onboarding = self.env['epo.onboarding'].sudo()
        for rec in self:
            rec.onboarding_id = Onboarding.search([
                ('employee_id', '=', rec.employee_id.id),
                ('project_id', '=', rec.project_id.id)], limit=1)

    @api.depends('phase_ids.phase', 'phase_ids.date_to', 'phase_ids.date_from')
    def _compute_current_phase(self):
        for rec in self:
            open_phase = rec.phase_ids.filtered(lambda p: not p.date_to).sorted('date_from')
            rec.current_phase = open_phase[-1].phase if open_phase else False

    @api.depends('phase_ids.duration_days', 'phase_ids.phase', 'date_from', 'date_to')
    def _compute_phase_stats(self):
        Entry = self.env['epo.form.entry'].sudo()
        for rec in self:
            phases = rec.phase_ids
            by_phase = {}
            for phase in phases:
                by_phase[phase.phase] = by_phase.get(phase.phase, 0) + phase.duration_days
            rec.days_onboarding = (by_phase.get('onboarding', 0) + by_phase.get('sop', 0))
            rec.days_training = by_phase.get('training', 0)
            rec.days_assessment = by_phase.get('assessment', 0)
            rec.days_tasking = by_phase.get('tasking', 0)
            end = rec.date_to or fields.Date.context_today(rec)
            rec.days_total = (end - rec.date_from).days + 1 if rec.date_from else 0
            first_tasking = phases.filtered(lambda p: p.phase == 'tasking').sorted('date_from')
            rec.days_to_productive = (
                (first_tasking[0].date_from - rec.date_from).days
                if first_tasking and rec.date_from else 0)
            rec.submission_count = Entry.search_count([
                ('employee_id', '=', rec.employee_id.id),
                ('project_id', '=', rec.project_id.id),
                ('state', '=', 'submitted'),
            ])

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    @api.constrains('employee_id', 'project_id', 'date_from', 'date_to')
    def _check_no_self_overlap(self):
        """The same person on the same project twice over overlapping dates is the
        classic double-count in every headcount report."""
        for rec in self:
            clash = self.search([
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
                ('project_id', '=', rec.project_id.id),
                ('date_from', '<=', rec.date_to or '9999-12-31'),
                '|', ('date_to', '=', False), ('date_to', '>=', rec.date_from),
            ], limit=1)
            if clash:
                raise ValidationError(_(
                    '%(name)s is already allocated to %(project)s over that period '
                    '(from %(from)s).',
                    name=rec.employee_id.display_name, project=rec.project_id.name,
                    **{'from': clash.date_from}))

    @api.constrains('employee_id', 'allocation_pct', 'date_from', 'date_to')
    def _check_capacity(self):
        """Part-time across two projects is legitimate; over the ceiling is someone
        being promised to two places at once."""
        ceiling = self._epo_capacity_ceiling()
        for rec in self:
            overlapping = self.search([
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
                ('date_from', '<=', rec.date_to or '9999-12-31'),
                '|', ('date_to', '=', False), ('date_to', '>=', rec.date_from),
            ])
            total = sum(overlapping.mapped('allocation_pct')) + rec.allocation_pct
            # Reads the configured ceiling. It previously hardcoded 100 while the
            # settings screen offered an editable maximum, so changing that setting
            # did nothing at all.
            if total > ceiling:
                raise ValidationError(_(
                    '%(name)s would be allocated %(total).0f%% of capacity over that '
                    'period, and the maximum is %(max).0f%%. Reduce a percentage or '
                    'close the other allocation first.',
                    name=rec.employee_id.display_name, total=total, max=ceiling))

    @api.model
    def _epo_capacity_ceiling(self):
        """The maximum combined allocation percentage, from settings.

        Defaults to 100 and is clamped to a sane range: a ceiling of 0 would make every
        allocation impossible, and a misconfigured setting must not be able to lock the
        whole organisation out of being staffed.
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'epo.allocation.max_pct', 100)
        try:
            ceiling = float(raw)
        except (TypeError, ValueError):
            return 100.0
        return ceiling if 1.0 <= ceiling <= 1000.0 else 100.0

    @api.constrains('employee_id', 'project_id', 'min_score_applied')
    def _check_meets_minimum_score(self):
        """A project's minimum score is a staffing rule, so it is checked when somebody
        is put on the project — not later, when they have already started reading the SOP.

        A PM can still allocate below the bar (a new joiner has no score at all, and
        somebody has to be first), but only with a reason, and the reason is audited.
        """
        for rec in self:
            bar = rec.min_score_applied
            if not bar or rec.override_reason:
                continue
            score = rec.score_at_allocation
            if score is None or score < bar:
                raise ValidationError(_(
                    '%(name)s has %(score)s and this project needs %(bar).0f. Lower the '
                    'project minimum, or allocate with a reason to override it.',
                    name=rec.employee_id.display_name,
                    score=(_('no assessment score yet') if not score
                           else _('scored %.0f') % score),
                    bar=bar))

    @api.constrains('project_id')
    def _check_project_live(self):
        """Allocating to a project that has not passed the go-live gate puts people on
        work that does not exist yet — no SOP to read, no stagelist to fill."""
        for rec in self:
            if rec.project_id.ethara_state != 'active':
                raise ValidationError(_(
                    'Project "%(name)s" is in %(state)s. Only a live project can take '
                    'people.', name=rec.project_id.name, state=rec.project_id.ethara_state))

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            project = self.env['project.project'].browse(vals['project_id'])
            employee = self.env['hr.employee'].browse(vals['employee_id'])
            vals.setdefault('min_score_applied', project.min_assessment_score)
            vals.setdefault('score_at_allocation',
                            employee._epo_best_assessment_score() or 0.0)
        allocations = super().create(vals_list)
        allocations._audit_overrides()
        Onboarding = self.env['epo.onboarding'].sudo()
        for alloc in allocations:
            # Joining a project always starts in onboarding — even for a veteran, the
            # SOP of THIS project has not been read yet.
            Onboarding._get(alloc.employee_id.id, alloc.project_id.id)
            self.env['epo.allocation.phase']._transition(
                alloc, 'onboarding', alloc.date_from)
            self.env['epo.timeline.event'].log(
                event_type='allocated', employee_id=alloc.employee_id.id,
                project_id=alloc.project_id.id, business_date=alloc.date_from,
                summary=_('Allocated to %(project)s at %(pct).0f%%',
                          project=alloc.project_id.name, pct=alloc.allocation_pct),
                payload={'role': alloc.role_on_project, 'from': str(alloc.date_from),
                         'min_score': alloc.min_score_applied,
                         'score': alloc.score_at_allocation},
                record=alloc)
        allocations._notify_allocated()
        return allocations

    def _audit_overrides(self):
        """Allocating below the bar is a decision, and decisions get recorded."""
        for alloc in self.filtered('override_reason'):
            self.env['epo.audit.log'].record(
                'allocation_below_minimum', alloc, alloc.override_reason,
                new_values={'employee': alloc.employee_id.display_name,
                            'project': alloc.project_id.name,
                            'minimum': alloc.min_score_applied,
                            'score': alloc.score_at_allocation})

    def _notify_allocated(self):
        """Tell the Tasker they are on a project, and who their lead is.

        Sent on joining because that is the moment they need to know: the onboarding
        gate is now open in front of them and nobody has told them yet. Failure to send
        is logged and swallowed — a mail server having a bad afternoon must not roll
        back the staffing decision.
        """
        template = self.env.ref(
            'ethara_project_os.mail_template_allocated', raise_if_not_found=False)
        if not template:
            return
        for alloc in self:
            if not alloc.employee_id.work_email:
                _logger.info(
                    'Project OS: no work email for %s, allocation notice not sent.',
                    alloc.employee_id.display_name)
                continue
            try:
                # The address is passed explicitly rather than left to the template.
                # A mail template is customer-editable data shipped with noupdate, so
                # its `To` can be edited, or left on Odoo's default-recipient mode,
                # and the notice would then go out addressed to nobody. Who this mail
                # is for is a fact of the allocation, not a formatting choice.
                template.sudo().send_mail(
                    alloc.id, force_send=False,
                    email_values={'email_to': alloc.employee_id.work_email})
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    'Project OS: could not queue the allocation notice for %s: %s',
                    alloc.employee_id.display_name, exc)

    def write(self, vals):
        if 'date_to' in vals and vals['date_to']:
            vals.setdefault('released_by_id', self.env.user.id)
        res = super().write(vals)
        if vals.get('date_to'):
            self._on_release()
        return res

    def unlink(self):
        for rec in self:
            if rec.phase_ids or rec.submission_count:
                raise UserError(_(
                    'This allocation already has history. Release it (set an end date) '
                    'instead of deleting it.'))
        return super().unlink()

    def _on_release(self):
        Phase = self.env['epo.allocation.phase'].sudo()
        for alloc in self:
            Phase._close_all(alloc, alloc.date_to)
            self.env['epo.timeline.event'].log(
                event_type='released', employee_id=alloc.employee_id.id,
                project_id=alloc.project_id.id, business_date=alloc.date_to,
                summary=_('Released from %(project)s after %(days)s days',
                          project=alloc.project_id.name, days=alloc.days_total),
                payload={'days_total': alloc.days_total,
                         'days_onboarding': alloc.days_onboarding,
                         'days_training': alloc.days_training,
                         'days_tasking': alloc.days_tasking},
                record=alloc)

    def action_release(self):
        """Close the membership today. The row and every phase under it stay."""
        today = fields.Date.context_today(self)
        for alloc in self:
            if alloc.date_to:
                continue
            alloc.write({'date_to': max(today, alloc.date_from)})
        return True

    @api.model
    def allocate_many(self, project_id, employee_ids, date_from=None, allocation_pct=100.0,
                      role_on_project='tasker', source='bulk', override_reason=None):
        """Bulk allocation — the PM's "put these twelve people on this project" action.

        Already-allocated people are skipped rather than raising, so one stale checkbox
        does not abort the whole batch."""
        project = self.env['project.project'].browse(project_id)
        if project.ethara_state != 'active':
            raise UserError(_(
                'Project "%(name)s" is in %(state)s. Activate it first — allocating to a '
                'project with no SOP or stagelist puts people on work that does not '
                'exist yet.', name=project.name, state=project.ethara_state))
        date_from = date_from or fields.Date.context_today(self)
        created = self.browse()
        skipped = []
        for employee_id in employee_ids:
            employee = self.env['hr.employee'].browse(employee_id)
            existing = self.search([
                ('employee_id', '=', employee_id), ('project_id', '=', project_id),
                ('date_to', '=', False)], limit=1)
            if existing:
                skipped.append(_('%(name)s — already on this project',
                                 name=employee.display_name))
                continue
            # One person who no longer qualifies must not abort the batch. A PM
            # selecting twelve people from a list that went stale while they read it
            # should get eleven allocations and a note, not an error and nothing.
            try:
                with self.env.cr.savepoint():
                    created |= self.create({
                        'project_id': project_id,
                        'employee_id': employee_id,
                        'date_from': date_from,
                        'allocation_pct': allocation_pct,
                        'role_on_project': role_on_project,
                        'source': source,
                        'override_reason': override_reason or False,
                    })
            except (UserError, ValidationError) as exc:
                skipped.append(f'{employee.display_name} — {exc}')
        return created, skipped


class EpoAllocationPhase(models.Model):
    """A contiguous stretch of one phase inside one allocation.

    Maintained by the roster, not by hand: when a lead sets somebody's status to
    Training, the open phase closes and a training phase opens. That is the whole
    mechanism behind "how long did onboarding take on this project" — the numbers
    accumulate from work people were already doing.
    """
    _name = 'epo.allocation.phase'
    _description = 'Project OS Allocation Phase'
    _order = 'allocation_id, date_from, id'
    _rec_name = 'display_name'

    allocation_id = fields.Many2one(
        'epo.allocation', required=True, ondelete='cascade', index=True)
    employee_id = fields.Many2one(
        related='allocation_id.employee_id', store=True, index=True)
    project_id = fields.Many2one(
        related='allocation_id.project_id', store=True, index=True)
    phase = fields.Selection(PHASES, required=True, index=True)
    date_from = fields.Date(required=True, default=fields.Date.context_today, index=True)
    date_to = fields.Date(index=True, help='Empty means this is the phase they are in now.')
    duration_days = fields.Integer(
        compute='_compute_duration', store=True,
        help='Inclusive day count. An open phase counts up to today.')
    note = fields.Text()

    _range_sane = models.Constraint(
        'CHECK (date_to IS NULL OR date_to >= date_from)',
        'A phase cannot end before it starts.')
    _one_open = models.UniqueIndex(
        '(allocation_id) WHERE date_to IS NULL',
        'An allocation can only be in one phase at a time.')
    # "In training and tasking on the same day" makes every duration figure meaningless.
    _no_overlap = models.Constraint(
        "EXCLUDE USING gist ("
        "allocation_id WITH =, daterange(date_from, date_to, '[]') WITH &&)",
        'Phases of one allocation cannot overlap.')

    @api.depends('date_from', 'date_to')
    def _compute_duration(self):
        today = fields.Date.context_today(self)
        for rec in self:
            end = rec.date_to or today
            rec.duration_days = ((end - rec.date_from).days + 1) if rec.date_from else 0

    @api.depends('phase', 'date_from', 'date_to')
    def _compute_display_name(self):
        labels = dict(PHASES)
        for rec in self:
            rec.display_name = '%s · %s → %s' % (
                labels.get(rec.phase, rec.phase), rec.date_from, rec.date_to or '…')

    # ------------------------------------------------------------------
    @api.model
    def _transition(self, allocation, phase, day):
        """Record that ``allocation`` was in ``phase`` on ``day``.

        Two paths, because leads work in two directions:

        * **Forward** — the ordinary case. Today's status moves the allocation on: the
          open phase closes yesterday and a new open phase starts today.
        * **Backfill** — a lead correcting last Tuesday. The day already sits inside a
          closed phase, so that phase is *split* around it rather than the current one
          being rewritten. Without this, correcting a past day silently rewrites the
          present and the day counts come out wrong.

        Idempotent in both directions: re-saving the same status for the same day does
        nothing, so a lead clicking save five times does not produce five phases.
        """
        if not phase or not allocation:
            return self.browse()
        day = day or fields.Date.context_today(self)

        # A phase can only exist inside its allocation's window. Without this, an event
        # that fires "now" — onboarding completing, say — writes a phase dated today
        # onto an allocation that was back-dated closed last Friday, and the phase then
        # ends before it starts. Clamp into the window, and record nothing at all for a
        # day the person was not on the project.
        if day < allocation.date_from:
            day = allocation.date_from
        if allocation.date_to and day > allocation.date_to:
            return self.browse()

        covering = self.search([
            ('allocation_id', '=', allocation.id),
            ('date_from', '<=', day),
            '|', ('date_to', '=', False), ('date_to', '>=', day),
        ], limit=1)

        if covering and covering.phase == phase:
            # Already in this phase on this day. Still worth a merge pass: the day
            # before may have just been backfilled with the same phase, and the two
            # stretches should read as one.
            return self._merge_adjacent(covering)

        if covering and not covering.date_to:
            # Forward move within the open phase.
            if covering.date_from >= day:
                # It started today: a zero-length phase is noise, so replace it.
                covering.write({'phase': phase})
                return self._merge_adjacent(covering)
            covering.write({'date_to': self._day_before(day)})
            # Flush before inserting the next phase. The ORM would otherwise defer this
            # UPDATE past the INSERT below, and for a moment two phases of the same
            # allocation would have no end date — exactly what `_one_open` forbids, so
            # a perfectly legal transition would fail.
            covering.flush_recordset(['date_to'])
            self._log_end(allocation, covering)
            return self._open(allocation, phase, day)

        if covering:
            # Backfill inside a closed stretch: split it around `day`.
            return self._split(allocation, covering, phase, day)

        # No phase covers this day. Either it is the start of the allocation, or the
        # person is resuming after a stretch off the project, or a lead is filling in a
        # gap left by leave. The first two open an ongoing phase; only a gap *between*
        # existing phases gets a one-day segment, so a correction to last Tuesday never
        # reopens a phase that has already ended.
        last = self.search(
            [('allocation_id', '=', allocation.id)], order='date_from desc', limit=1)
        resuming = not last or day > (last.date_to or last.date_from)
        return self._open(allocation, phase, day,
                          date_to=None if resuming else day)

    @api.model
    def _split(self, allocation, covering, phase, day):
        """Carve `day` out of an existing closed phase and give it its own segment.

        Order matters: shrink the head first and flush it, or the new segments collide
        with the range they are being carved out of.
        """
        head_from, tail_to = covering.date_from, covering.date_to
        original_phase = covering.phase

        if head_from == day and tail_to == day:
            covering.write({'phase': phase})
            return self._merge_adjacent(covering)

        if head_from < day:
            covering.write({'date_to': self._day_before(day)})
        else:
            # `day` is the first day of the stretch: the head is empty, so the covering
            # row becomes the tail and a fresh segment takes the day.
            covering.write({'date_from': day + timedelta(days=1)})
        covering.flush_recordset(['date_from', 'date_to'])

        if head_from < day < tail_to:
            self.create({
                'allocation_id': allocation.id, 'phase': original_phase,
                'date_from': day + timedelta(days=1), 'date_to': tail_to,
            }).flush_recordset()

        return self._open(allocation, phase, day, date_to=day)

    @api.model
    def _merge_adjacent(self, phase_record):
        """Join a segment to a neighbour of the same phase that it touches.

        Without this, a lead catching up on a week of roster produced seven one-day
        rows of "tasking" instead of one stretch. The day counts were right either way,
        but the phase log is meant to read as "training Jul 19 → Jul 21", not as a
        diary entry per day.
        """
        if not phase_record:
            return phase_record

        previous = self.search([
            ('allocation_id', '=', phase_record.allocation_id.id),
            ('phase', '=', phase_record.phase),
            ('date_to', '=', self._day_before(phase_record.date_from)),
        ], limit=1)
        if previous:
            end = phase_record.date_to
            phase_record.with_context(epo_phase_cleanup=True).unlink()
            self.env.flush_all()
            previous.write({'date_to': end})
            previous.flush_recordset(['date_to'])
            phase_record = previous

        if phase_record.date_to:
            following = self.search([
                ('allocation_id', '=', phase_record.allocation_id.id),
                ('phase', '=', phase_record.phase),
                ('date_from', '=', phase_record.date_to + timedelta(days=1)),
            ], limit=1)
            if following:
                end = following.date_to
                following.with_context(epo_phase_cleanup=True).unlink()
                self.env.flush_all()
                phase_record.write({'date_to': end})
                phase_record.flush_recordset(['date_to'])
        return phase_record

    @api.model
    def _open(self, allocation, phase, day, date_to=None):
        new_phase = self.create({
            'allocation_id': allocation.id, 'phase': phase,
            'date_from': day, 'date_to': date_to,
        })
        self.env['epo.timeline.event'].log(
            event_type='phase_started', employee_id=allocation.employee_id.id,
            project_id=allocation.project_id.id, business_date=day,
            summary=_('%s started', dict(PHASES)[phase]), record=new_phase)
        return self._merge_adjacent(new_phase)

    @api.model
    def _log_end(self, allocation, phase_record):
        self.env['epo.timeline.event'].log(
            event_type='phase_ended', employee_id=allocation.employee_id.id,
            project_id=allocation.project_id.id, business_date=phase_record.date_to,
            summary=_('%(phase)s ended after %(days)s days',
                      phase=dict(PHASES)[phase_record.phase],
                      days=phase_record.duration_days),
            record=phase_record)

    @api.model
    def _day_before(self, day):
        return day - timedelta(days=1)

    @api.model
    def _close_all(self, allocation, day):
        """Close every open phase when the allocation ends.

        Releases are routinely back-dated — "they came off the project last Friday",
        entered on Monday. Any phase that started after that date describes time the
        person did not spend on this project, so it is removed rather than squashed
        into a zero-length segment that would still show up in the day counts.
        """
        day = day or fields.Date.context_today(self)
        phases = self.search([('allocation_id', '=', allocation.id)])

        # Anything that starts after the end date describes time the person did not
        # spend on this project. Remove it rather than squash it into a zero-length
        # segment that would still appear in the day counts.
        stale = phases.filtered(lambda p: p.date_from > day)
        if stale:
            stale.with_context(epo_phase_cleanup=True).unlink()
            phases -= stale

        # Anything still running, or already closed past the end date, is trimmed back
        # to it. A phase closed at the time of an earlier transition does not know that
        # the allocation would later be back-dated, so trimming here is what keeps the
        # phase breakdown adding up to "days on project".
        overrunning = phases.filtered(lambda p: not p.date_to or p.date_to > day)
        overrunning.write({'date_to': day})
        overrunning.flush_recordset(['date_to'])
        return overrunning

    @api.model
    def _close_open_for(self, employee_id, day):
        """Close whatever phase this person is in on ``day`` — used when the roster says
        they are on leave, benched or blocked, so non-project time never counts as
        project time.

        Only phases that were already running on that day are touched. A phase that
        starts *later* is nothing to do with this day: closing it anyway is how a lead
        catching up on last week's roster silently ended the stretch somebody is in
        right now, leaving them with no current phase at all.
        """
        touched = self.browse()
        for phase in self.search([('employee_id', '=', employee_id),
                                  ('date_to', '=', False)]):
            if phase.date_from > day:
                continue                      # starts after this day — unrelated
            if phase.date_from == day:
                # It would cover only a day the person was not on the project.
                phase.with_context(epo_phase_cleanup=True).unlink()
                continue
            phase.write({'date_to': self._day_before(day)})
            touched |= phase
        touched.flush_recordset(['date_to'])
        return touched

    def unlink(self):
        """Phase history is evidence of how long things took.

        The single exception is internal cleanup of a segment that fell outside its
        allocation's window (see :meth:`_close_all`) — that is an artifact, not history.
        """
        if not self.env.context.get('epo_phase_cleanup'):
            raise UserError(_(
                'Phase history is the record of how long things took. Correct the '
                'roster instead of deleting the phase.'))
        return super().unlink()
