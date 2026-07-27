"""The daily roster — one row per person per business day.

This is the operational spine. It answers *"what is this person doing today"*, which a
date-ranged allocation cannot: someone allocated to a project for three months is, on
any given day, tasking or in training or on leave or blocked.

Deliberately distinct from ``epo.allocation`` ("who is on this project, since when").
Conflating the two is the single most common design error in this domain — you end up
either unable to say who worked yesterday, or rewriting the allocation every morning.
The roster's project defaults *from* the open allocation; it never replaces it.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

TASKING_STATUS = [
    ('tasking', 'Tasking'),
    ('onboarding', 'Onboarding'),
    ('training', 'Training'),
    ('assessment', 'Assessment'),
    ('leave', 'Leave'),
    ('unable_to_task', 'Unable to Task'),
    ('bench', 'Bench'),
]

# Roster status → the allocation phase it represents. Statuses absent from this map
# (leave, unable_to_task, bench) do not open a phase: time not worked on the project
# must not inflate "days spent on this project".
STATUS_TO_PHASE = {
    'tasking': 'tasking',
    'onboarding': 'onboarding',
    'training': 'training',
    'assessment': 'assessment',
}


class EpoRosterDay(models.Model):
    _name = 'epo.roster.day'
    _description = 'Project OS Tasking Board'
    _order = 'business_date desc, employee_id'
    _rec_name = 'display_name'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True)
    business_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True,
        help='The business day in the company timezone — not UTC. Getting this wrong '
             'silently shifts a night shift by a day.')
    pod_id = fields.Many2one(
        'epo.pod', related='employee_id.epo_pod_id', store=True, index=True)
    project_id = fields.Many2one(
        'project.project', ondelete='restrict', index=True,
        domain=[('is_project_os', '=', True)],
        help='Defaults from the open allocation. Blank means bench or leave.')
    tasking_status = fields.Selection(TASKING_STATUS, required=True, default='tasking')
    present = fields.Boolean(default=False, help='Set by attendance check-in.')
    issue = fields.Text(help='Required when the status is "Unable to Task".')
    source = fields.Selection(
        [('manual', 'Manual'), ('bulk', 'Bulk'), ('carry_forward', 'Carry forward'),
         ('leave_sync', 'Leave sync'), ('attendance', 'Attendance')],
        required=True, default='manual')
    locked = fields.Boolean(
        default=False,
        help='Payroll cut-off. A locked day cannot be edited except by an Admin '
             'unlocking it first, which is audited.')

    # Read off the onboarding record for this person on this project. Not a related
    # field: roster.day is keyed (employee, date) and onboarding is keyed
    # (employee, project), so there is no single m2o to traverse — the pair has to be
    # looked up. Unstored, because it is display-only and the gate itself is enforced on
    # epo.form.entry, not here.
    onboarding_unlocked = fields.Boolean(
        compute='_compute_onboarding_state', search='_search_onboarding_unlocked',
        string='Cleared to task',
        help='Whether this person has finished onboarding on the project they are '
             'rostered to today.')
    onboarding_blockers = fields.Char(
        compute='_compute_onboarding_state', string='Onboarding outstanding',
        help='What is still missing before they may fill the stagelist. Empty once they '
             'are cleared.')

    _one_per_day = models.Constraint(
        'UNIQUE (employee_id, business_date)',
        'That person already has a roster row for that day.')
    _issue_required = models.Constraint(
        "CHECK (tasking_status <> 'unable_to_task' OR issue IS NOT NULL)",
        '"Unable to Task" needs a reason — an unexplained roster hole is unauditable.')
    _leave_unprojected = models.Constraint(
        "CHECK (tasking_status <> 'leave' OR project_id IS NULL)",
        'Someone on leave cannot also be on a project that day.')

    @api.depends('employee_id', 'business_date')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.employee_id.display_name} · {rec.business_date or ""}'

    @api.depends('employee_id', 'project_id')
    def _compute_onboarding_state(self):
        """So a Pod Lead can see "not cleared yet" on the board they are already in,
        instead of holding two screens side by side."""
        Onboarding = self.env['epo.onboarding'].sudo()
        for rec in self:
            if not rec.project_id:
                rec.onboarding_unlocked = False
                rec.onboarding_blockers = ''
                continue
            record = Onboarding.search([
                ('employee_id', '=', rec.employee_id.id),
                ('project_id', '=', rec.project_id.id)], limit=1)
            rec.onboarding_unlocked = bool(record.unlocked)
            rec.onboarding_blockers = record.blockers or ''

    def _search_onboarding_unlocked(self, operator, value):
        """Make the board's "Not cleared to task" filter work.

        The field is computed and unstored, so without this the filter cannot exist at
        all. Resolved by collecting the (employee, project) pairs that ARE unlocked and
        matching roster rows against them — a row with no project is never "cleared",
        because there is nothing to be cleared for.
        """
        if operator not in ('=', '!='):
            raise UserError(_(
                'Cannot filter "cleared to task" with the operator "%s".', operator))
        wanted = bool(value) if operator == '=' else not bool(value)

        cleared = self.env['epo.onboarding'].sudo().search([('unlocked', '=', True)])
        pairs = {(r.employee_id.id, r.project_id.id) for r in cleared}
        rows = self.sudo().search([('project_id', '!=', False)])
        matching = [
            r.id for r in rows
            if ((r.employee_id.id, r.project_id.id) in pairs) is wanted
        ]
        return [('id', 'in', matching)]

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    @api.constrains('business_date')
    def _check_not_far_future(self):
        """Planning belongs to the allocation; the roster is a record of today.
        Tomorrow is allowed so a lead can prepare the next shift."""
        limit = fields.Date.context_today(self) + timedelta(days=1)
        for rec in self:
            if rec.business_date and rec.business_date > limit:
                raise ValidationError(_(
                    'Roster date %(date)s is beyond tomorrow. Use an allocation to plan '
                    'ahead — the roster records what is happening now.',
                    date=rec.business_date))

    @api.constrains('project_id', 'employee_id', 'business_date')
    def _check_allocated(self):
        """Rostering someone onto a project they are not allocated to is how phantom
        headcount gets into a delivery report."""
        for rec in self:
            if not rec.project_id:
                continue
            if not rec._allocation_for_day():
                raise ValidationError(_(
                    '%(name)s is not allocated to %(project)s on %(date)s. Allocate them '
                    'first — the allocation is what the project bills and reports on.',
                    name=rec.employee_id.display_name,
                    project=rec.project_id.display_name, date=rec.business_date))

    def _allocation_for_day(self):
        self.ensure_one()
        return self.env['epo.allocation'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('project_id', '=', self.project_id.id),
            ('date_from', '<=', self.business_date),
            '|', ('date_to', '=', False), ('date_to', '>=', self.business_date),
        ], limit=1)

    def _assert_unlocked(self):
        for rec in self:
            if rec.locked:
                raise UserError(_(
                    'The roster for %(name)s on %(date)s is locked (payroll closed). '
                    'An Admin must unlock it first, with a reason.',
                    name=rec.employee_id.display_name, date=rec.business_date))

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('project_id') and vals.get('employee_id'):
                vals.setdefault('project_id', self._default_project(
                    vals['employee_id'],
                    vals.get('business_date') or fields.Date.context_today(self)))
        recs = super().create(vals_list)
        recs._sync_phase()
        recs._log()
        return recs

    def write(self, vals):
        if not (set(vals) <= {'locked'}):
            self._assert_unlocked()
        res = super().write(vals)
        if {'tasking_status', 'project_id'} & set(vals):
            self._sync_phase()
            self._log()
        return res

    def unlink(self):
        """A roster row is a record of a day that happened. Correct it, don't erase it."""
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_admin')):
            raise UserError(_(
                'Roster rows are the record of a working day. Change the status instead '
                'of deleting the row.'))
        return super().unlink()

    @api.model
    def _default_project(self, employee_id, day):
        alloc = self.env['epo.allocation'].sudo().search([
            ('employee_id', '=', employee_id),
            ('date_from', '<=', day),
            '|', ('date_to', '=', False), ('date_to', '>=', day),
        ], order='date_from desc', limit=1)
        return alloc.project_id.id or False

    def _log(self):
        Timeline = self.env['epo.timeline.event'].sudo()
        for rec in self:
            Timeline.log(
                event_type='roster_set',
                employee_id=rec.employee_id.id,
                project_id=rec.project_id.id or None,
                business_date=rec.business_date,
                summary=_('Roster: %(status)s', status=dict(TASKING_STATUS)[rec.tasking_status]),
                payload={'status': rec.tasking_status, 'present': rec.present,
                         'issue': rec.issue or '', 'source': rec.source},
                record=rec,
            )

    def _sync_phase(self):
        """Keep the allocation phase segments in step with the roster.

        This is what makes "how long was this person in training on this project?"
        answerable without anyone maintaining a second record by hand: the lead sets a
        status each day, and the phase log accumulates underneath."""
        Phase = self.env['epo.allocation.phase'].sudo()
        for rec in self:
            phase = STATUS_TO_PHASE.get(rec.tasking_status)
            alloc = rec._allocation_for_day() if rec.project_id else False
            if not alloc:
                Phase._close_open_for(rec.employee_id.id, rec.business_date)
                continue
            Phase._transition(alloc, phase, rec.business_date)

    # ------------------------------------------------------------------
    # helpers used by the API and the crons
    # ------------------------------------------------------------------
    @api.model
    def upsert(self, employee_id, day, values, source='manual'):
        """One row per person per day, created or patched. The API never inserts
        blindly — a double-tapped save must not produce two rows."""
        day = day or fields.Date.context_today(self)
        row = self.search([
            ('employee_id', '=', employee_id), ('business_date', '=', day)], limit=1)
        if row:
            row.write(values)
            return row
        return self.create(dict(values, employee_id=employee_id,
                                business_date=day, source=source))

    @api.model
    def _cron_carry_forward(self):
        """Clone yesterday's roster into today for every active employee that does not
        have a row yet. Without this the roster is empty every morning and the whole
        system looks broken until someone fills it in by hand — the prototype's most
        visible operational gap."""
        today = fields.Date.context_today(self)
        yesterday = today - timedelta(days=1)
        existing = set(self.search([('business_date', '=', today)]).mapped('employee_id').ids)
        previous = self.search([('business_date', '=', yesterday)])
        created = self.browse()
        for row in previous:
            if row.employee_id.id in existing or not row.employee_id.active:
                continue

            # Resolve today's project from the allocation rather than copying
            # yesterday's. Somebody released last night is no longer on that project,
            # and carrying the stale project forward made the row fail its own
            # allocation check — which aborted the cron and left the WHOLE
            # organisation with an empty board the next morning.
            project = self._default_project(row.employee_id.id, today)
            status = row.tasking_status
            if not project:
                status = 'bench'
            elif status in ('leave', 'unable_to_task'):
                # Yesterday's exception is not today's fact.
                status = 'tasking'

            # One unhappy row must not cost everybody else their roster.
            try:
                with self.env.cr.savepoint():
                    created |= self.create({
                        'employee_id': row.employee_id.id,
                        'business_date': today,
                        'project_id': project,
                        'tasking_status': status,
                        'present': False,
                        'source': 'carry_forward',
                    })
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    'Project OS: could not carry the roster forward for %s: %s',
                    row.employee_id.display_name, exc)
        return len(created)

    @api.model
    def _cron_lock_closed_days(self, days=7):
        """Lock everything older than the payroll window so retro-edits stop being
        possible by accident."""
        cutoff = fields.Date.context_today(self) - timedelta(days=days)
        rows = self.search([('business_date', '<', cutoff), ('locked', '=', False)])
        rows.write({'locked': True})
        return len(rows)

    def action_unlock(self):
        """Admin-only, audited. The reason comes from the context so the same method
        serves the UI button and the API."""
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_admin')):
            raise UserError(_('Only an Admin may unlock a payroll-closed roster day.'))
        reason = self.env.context.get('epo_reason')
        if not reason:
            raise UserError(_('Unlocking a closed day needs a reason.'))
        for rec in self:
            self.env['epo.audit.log'].record(
                'roster_unlock', rec, reason, old_values={'locked': True})
        return self.write({'locked': False})
