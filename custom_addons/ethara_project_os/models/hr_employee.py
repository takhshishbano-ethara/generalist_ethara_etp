"""The person.

Ethara Project OS does not keep its own people table — ``hr.employee`` is the person,
and this file adds the project-side facts to it: which pod they sit in, which seat,
what their current Project OS role is, which projects they have been on and for how
long.

Employees are never hard-deleted. Allocations, submissions and assessment results
reference them for the lifetime of those records; offboarding archives the employee
instead.
"""

import operator as py_operator

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .epo_role_assignment import ROLE_RANK

# The domain operators `_search_best_score` knows how to answer. Anything outside this
# set (`like`, `child_of`, …) is meaningless on a score, so it is refused rather than
# silently answered wrong.
SCORE_OPERATORS = {
    '=': py_operator.eq,
    '!=': py_operator.ne,
    '<': py_operator.lt,
    '<=': py_operator.le,
    '>': py_operator.gt,
    '>=': py_operator.ge,
    'in': lambda score, value: score in value,
    'not in': lambda score, value: score not in value,
}


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # --- placement -----------------------------------------------------
    epo_pod_id = fields.Many2one(
        'epo.pod', string='Pod', ondelete='set null', index=True, tracking=True,
        help='Seating / supervision group. Never required — a new joiner has no pod yet.')
    epo_pod_lead_id = fields.Many2one(
        'hr.employee', string='Pod Lead', related='epo_pod_id.lead_employee_id', store=True)
    epo_seat_no = fields.Char(string='Seat')
    epo_job_status = fields.Selection(
        [('fte', 'FTE'), ('contract', 'Contract'), ('intern', 'Intern'), ('vendor', 'Vendor')],
        string='Engagement', default='fte')

    # --- role ----------------------------------------------------------
    epo_role_assignment_ids = fields.One2many(
        'epo.role.assignment', 'employee_id', string='Role Grants')
    epo_role = fields.Selection(
        [('tasker', 'Tasker'), ('pl', 'PL — Pod Lead'),
         ('pm', 'PM'), ('admin', 'Admin')],
        string='Project OS Role', compute='_compute_epo_role', store=True, index=True,
        help='The strongest role currently granted. Derived from the effective-dated '
             'grants, never set by hand.')

    # --- project life --------------------------------------------------
    epo_allocation_ids = fields.One2many('epo.allocation', 'employee_id', string='Allocations')
    epo_current_allocation_id = fields.Many2one(
        'epo.allocation', compute='_compute_current_allocation',
        string='Current Allocation')
    # Stored, and therefore on its own compute: mixing a stored and a non-stored field
    # in one method makes reading either one recompute and write the other.
    epo_current_project_id = fields.Many2one(
        'project.project', compute='_compute_current_project', store=True,
        string='Current Project',
        help='The project of the open allocation. Empty on the bench.')
    epo_allowed_project_ids = fields.Many2many(
        'project.project', compute='_compute_allowed_projects',
        string='Today\'s projects',
        help='The projects this person may act on today: every allocation whose window '
             'covers today, on a project that is live. This is what a client should '
             'drive a project picker from — rostering or submitting against anything '
             'else is refused further down, and offering it is how a screen sets '
             'somebody up to fail.')
    epo_project_count = fields.Integer(compute='_compute_project_stats', string='Projects')
    epo_onboarding_ids = fields.One2many('epo.onboarding', 'employee_id', string='Onboarding')
    epo_timeline_ids = fields.One2many('epo.timeline.event', 'employee_id', string='History')
    epo_roster_ids = fields.One2many('epo.roster.day', 'employee_id', string='Roster')

    # --- rolled-up time ------------------------------------------------
    epo_days_onboarding = fields.Integer(
        compute='_compute_project_stats', string='Days Onboarding',
        help='Total days this person has spent in onboarding phases, across every project.')
    epo_days_tasking = fields.Integer(
        compute='_compute_project_stats', string='Days Tasking')
    epo_submission_count = fields.Integer(
        compute='_compute_project_stats', string='Submissions')

    @api.depends('epo_role_assignment_ids.is_current', 'epo_role_assignment_ids.role')
    def _compute_epo_role(self):
        for emp in self:
            roles = emp.epo_role_assignment_ids.filtered('is_current').mapped('role')
            emp.epo_role = max(roles, key=ROLE_RANK.index) if roles else False

    def _epo_open_allocation(self):
        """The allocation the person is on right now, or an empty recordset."""
        self.ensure_one()
        live = self.epo_allocation_ids.filtered(lambda a: not a.date_to)
        return live.sorted('date_from')[-1:] if live else self.epo_allocation_ids.browse()

    @api.depends('epo_allocation_ids.date_to', 'epo_allocation_ids.date_from')
    def _compute_current_allocation(self):
        for emp in self:
            emp.epo_current_allocation_id = emp._epo_open_allocation()

    @api.depends('epo_allocation_ids.date_to', 'epo_allocation_ids.date_from',
                 'epo_allocation_ids.project_id')
    def _compute_current_project(self):
        for emp in self:
            emp.epo_current_project_id = emp._epo_open_allocation().project_id

    def _compute_allowed_projects(self):
        """Every project whose allocation covers today — not just the latest one.

        Part-time membership across two projects is legitimate (``allocation_pct``
        exists precisely for it), so "what may this person work on today" is a list,
        not the single ``epo_current_project_id``.
        """
        today = fields.Date.context_today(self)
        for emp in self:
            covering = emp.epo_allocation_ids.filtered(
                lambda a: a.date_from <= today and (not a.date_to or a.date_to >= today))
            emp.epo_allowed_project_ids = covering.project_id.filtered(
                lambda p: p.ethara_state == 'active')

    def _compute_project_stats(self):
        Phase = self.env['epo.allocation.phase'].sudo()
        Entry = self.env['epo.form.entry'].sudo()
        for emp in self:
            emp.epo_project_count = len(emp.epo_allocation_ids.mapped('project_id'))
            onboarding = Phase.search([
                ('employee_id', '=', emp.id),
                ('phase', 'in', ('onboarding', 'sop', 'training', 'assessment')),
            ])
            tasking = Phase.search([('employee_id', '=', emp.id), ('phase', '=', 'tasking')])
            emp.epo_days_onboarding = sum(onboarding.mapped('duration_days'))
            emp.epo_days_tasking = sum(tasking.mapped('duration_days'))
            emp.epo_submission_count = Entry.search_count([
                ('employee_id', '=', emp.id), ('state', '=', 'submitted')])

    def _epo_best_assessment_score(self):
        """The best score this person has been verified at, across every project.

        Odoo does not grade — a Tasker reads the external result and records what they saw
        on the onboarding record (v2 §4.6.3). This reads those.

        ``None`` when nobody has ever recorded a score, which is deliberately different
        from zero: "never assessed" and "assessed and scored nothing" are different
        facts, and a staffing screen has to be able to say which.
        """
        self.ensure_one()
        rows = self.env['epo.onboarding'].sudo().search(
            [('employee_id', '=', self.id), ('assessment_passed', '=', True),
             ('assessment_score', '>', 0)],
            order='assessment_score desc', limit=1)
        return rows.assessment_score if rows else None

    epo_best_assessment_score = fields.Float(
        compute='_compute_best_score', search='_search_best_score', string='Best score',
        help='Highest graded assessment score across every project. What the minimum '
             'score on a project is checked against.')

    def _compute_best_score(self):
        for employee in self:
            employee.epo_best_assessment_score = employee._epo_best_assessment_score() or 0.0

    def _search_best_score(self, operator, value):
        """Resolve a domain on the best score against the verified onboarding rows.

        The field is computed and unstored, so without this a search view cannot filter
        on it at all — which is what the "Never assessed" filter needs to do. The one
        subtlety is the ungraded: ``_compute_best_score`` shows them as ``0.0``, so a
        domain they satisfy has to match them too even though they own no result row.
        """
        rows = self.env['epo.onboarding'].sudo()._read_group(
            [('assessment_passed', '=', True), ('assessment_score', '>', 0)],
            ['employee_id'], ['assessment_score:max'])
        best = {employee.id: score or 0.0 for employee, score in rows}

        compare = SCORE_OPERATORS.get(operator)
        if compare is None:
            raise UserError(_('Cannot filter the best score with the operator "%s".', operator))

        matching = [eid for eid, score in best.items() if compare(score, value)]
        if compare(0.0, value):
            # The ungraded read as zero, so they belong in the result set as well.
            return ['|', ('id', 'in', matching), ('id', 'not in', list(best))]
        return [('id', 'in', matching)]

    @api.constrains('active', 'departure_date')
    def _check_exit_recorded(self):
        """An archived employee with no exit date disappears from the roster with no
        audit answer to "when did they leave?"."""
        for emp in self:
            if not emp.active and not emp.departure_date:
                raise ValidationError(_(
                    'Set a departure date on %(name)s before archiving — the roster and '
                    'the allocation history need to know when they left.',
                    name=emp.display_name))

    def action_epo_open_history(self):
        """Everything this person has done, the same rows the API serves at
        GET /employees/<id>/history."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('History — %s', self.display_name),
            'res_model': 'epo.timeline.event',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
        }

    # ------------------------------------------------------------------
    # scoping helpers — used by the REST layer and by the record rules
    # ------------------------------------------------------------------
    def _epo_scope_employee_ids(self):
        """The employee ids this employee is allowed to see, by their role.

        Tasker    → themselves
        PL    → themselves + everyone in the pods they lead (+ direct reports)
        PM   → everyone
        Admin → everyone
        """
        self.ensure_one()
        if self.epo_role in ('pm', 'admin'):
            return self.env['hr.employee'].sudo().search([]).ids
        if self.epo_role == 'pl':
            pods = self.env['epo.pod'].sudo().search([('lead_employee_id', '=', self.id)])
            peers = self.env['hr.employee'].sudo().search([
                '|', ('epo_pod_id', 'in', pods.ids), ('parent_id', '=', self.id)])
            return (peers | self).ids
        return self.ids
