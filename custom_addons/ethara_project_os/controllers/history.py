"""History and analytics.

The two questions this module was built to answer, served from the same rows:

* ``GET /employees/<id>/history`` — *a person's full history*: every project they
  joined, how long they spent in onboarding, training, assessment and tasking on each,
  every role grant, every assessment attempt, every submission count.
* ``GET /projects/<id>/history`` — *a project's full history*: who joined when, who is
  at which phase right now, how long the average person took to become productive, and
  the full event stream from creation to archive.

Neither is a stored report. Both are aggregates over the timeline and the phase log, so
they cannot drift from what actually happened.
"""

from odoo import fields, http
from odoo.http import request

from .utils import opt_int, respond, route

PHASE_LABELS = {
    'onboarding': 'Onboarding', 'sop': 'SOP reading', 'training': 'Training',
    'assessment': 'Assessment', 'ramp_up': 'Ramp-up', 'tasking': 'Tasking',
    'ramp_down': 'Ramp-down',
}


def _event_dict(event):
    return {
        'id': event.id,
        'occurred_at': str(event.occurred_at),
        'business_date': str(event.business_date or ''),
        'event_type': event.event_type,
        'category': event.category,
        'summary': event.summary,
        'employee_id': event.employee_id.id or None,
        'employee_name': event.employee_id.display_name or '',
        'project_id': event.project_id.id or None,
        'project_name': event.project_id.name or '',
        'actor': event.actor_id.name or '',
        'payload': event.payload_json or '',
    }


def _phase_breakdown(phases):
    """Days per phase, in a shape a chart can consume directly."""
    totals = {}
    for phase in phases:
        totals.setdefault(phase.phase, {'days': 0, 'stretches': 0,
                                        'label': PHASE_LABELS.get(phase.phase, phase.phase)})
        totals[phase.phase]['days'] += phase.duration_days
        totals[phase.phase]['stretches'] += 1
    return [{'phase': key, **value} for key, value in totals.items()]


class ProjectOsHistory(http.Controller):

    # ------------------------------------------------------------------
    # a person
    # ------------------------------------------------------------------
    @route('/api/project-os/employees/<int:employee_id>/history', ['GET'])
    def employee_history(self, ctx, employee_id):
        """One person, end to end: every project, every phase, every event."""
        ctx.assert_can_see_employee(employee_id)
        employee = ctx.env['hr.employee'].sudo().browse(employee_id).exists()
        if not employee:
            return respond(message='No such employee.', status=404)

        allocations = ctx.env['epo.allocation'].sudo().search([
            ('employee_id', '=', employee_id)], order='date_from')
        Entry = ctx.env['epo.form.entry'].sudo()
        projects = []
        for allocation in allocations:
            projects.append({
                'allocation_id': allocation.id,
                'project_id': allocation.project_id.id,
                'project_name': allocation.project_id.name,
                'project_state': allocation.project_id.ethara_state,
                'role_on_project': allocation.role_on_project,
                'joined': str(allocation.date_from),
                'left': str(allocation.date_to or ''),
                'is_open': allocation.is_open,
                'allocation_pct': allocation.allocation_pct,
                'current_phase': allocation.current_phase or '',
                'days_total': allocation.days_total,
                'days_to_productive': allocation.days_to_productive,
                'phases': _phase_breakdown(allocation.phase_ids),
                'phase_log': [{
                    'phase': p.phase, 'label': PHASE_LABELS.get(p.phase, p.phase),
                    'from': str(p.date_from), 'to': str(p.date_to or ''),
                    'days': p.duration_days,
                } for p in allocation.phase_ids.sorted('date_from')],
                'onboarding': {
                    'unlocked': allocation.onboarding_id.unlocked,
                    'blockers': allocation.onboarding_id.blockers or '',
                    'sop_done_at': str(allocation.onboarding_id.sop_done_at or ''),
                    'training_done_at': str(allocation.onboarding_id.training_done_at or ''),
                    'unlocked_at': str(allocation.onboarding_id.unlocked_at or ''),
                    'waived': bool(allocation.onboarding_id.waived_by_id),
                } if allocation.onboarding_id else None,
                'submissions': Entry.search_count([
                    ('employee_id', '=', employee_id),
                    ('project_id', '=', allocation.project_id.id),
                    ('state', '=', 'submitted')]),
            })

        roles = ctx.env['epo.role.assignment'].sudo().search([
            ('employee_id', '=', employee_id)], order='date_from')
        verified = ctx.env['epo.onboarding'].sudo().search(
            [('employee_id', '=', employee_id), ('assessment_passed', '=', True)],
            order='assessment_verified_at')
        events = ctx.env['epo.timeline.event'].sudo().search(
            [('employee_id', '=', employee_id)],
            limit=opt_int(request.params, 'limit', 300, minimum=1, maximum=1000))

        all_phases = allocations.mapped('phase_ids')
        return respond({
            'employee': {
                'id': employee.id, 'name': employee.display_name,
                'email': employee.work_email or '',
                'pod': employee.epo_pod_id.display_name or '',
                'pod_lead': employee.epo_pod_lead_id.display_name or '',
                'seat': employee.epo_seat_no or '',
                'engagement': employee.epo_job_status or '',
                'current_role': employee.epo_role or '',
                'joined': str(employee.create_date or ''),
                'active': employee.active,
            },
            'totals': {
                'projects': len(allocations.mapped('project_id')),
                'submissions': Entry.search_count([
                    ('employee_id', '=', employee_id), ('state', '=', 'submitted')]),
                'days_by_phase': _phase_breakdown(all_phases),
            },
            'projects': projects,
            'roles': [{
                'role': r.role, 'pod': r.scope_pod_id.display_name or '',
                'from': str(r.date_from), 'to': str(r.date_to or ''),
                'current': r.is_current, 'granted_by': r.granted_by_id.display_name or '',
                'reason': r.reason or '',
            } for r in roles],
            # Odoo does not grade — these are the verifications a PM recorded after
            # reading each result in the external application.
            'assessments': [{
                'project': v.project_id.name,
                'passed': v.assessment_passed,
                'score': v.assessment_score or None,
                'verified_by': v.assessment_verified_by.name or '',
                'verified_at': str(v.assessment_verified_at or ''),
            } for v in verified],
            'timeline': [_event_dict(e) for e in events],
        })

    # ------------------------------------------------------------------
    # a project
    # ------------------------------------------------------------------
    @route('/api/project-os/projects/<int:project_id>/history', ['GET'])
    def project_history(self, ctx, project_id):
        """One project, end to end: who was on it, at what phase, and what happened."""
        ctx.assert_can_see_project(project_id)
        project = ctx.env['project.project'].sudo().browse(project_id).exists()
        if not project or not project.is_project_os:
            return respond(message='No such project.', status=404)

        allocations = project.allocation_ids.sorted('date_from')
        Entry = ctx.env['epo.form.entry'].sudo()
        people = [{
            'allocation_id': a.id,
            'employee_id': a.employee_id.id,
            'employee_name': a.employee_id.display_name,
            'pod': a.pod_id.display_name or '',
            'role_on_project': a.role_on_project,
            'joined': str(a.date_from),
            'left': str(a.date_to or ''),
            'is_open': a.is_open,
            'current_phase': a.current_phase or '',
            'onboarding_unlocked': a.onboarding_id.unlocked,
            'days_total': a.days_total,
            'days_onboarding': a.days_onboarding,
            'days_training': a.days_training,
            'days_assessment': a.days_assessment,
            'days_tasking': a.days_tasking,
            'days_to_productive': a.days_to_productive,
            'submissions': Entry.search_count([
                ('employee_id', '=', a.employee_id.id), ('project_id', '=', project_id),
                ('state', '=', 'submitted')]),
        } for a in allocations]

        productive = [p['days_to_productive'] for p in people if p['days_to_productive']]
        phases = allocations.mapped('phase_ids')
        by_phase_now = {}
        for allocation in allocations.filtered('is_open'):
            key = allocation.current_phase or 'unassigned'
            by_phase_now[key] = by_phase_now.get(key, 0) + 1

        events = ctx.env['epo.timeline.event'].sudo().search(
            [('project_id', '=', project_id)],
            limit=opt_int(request.params, 'limit', 300, minimum=1, maximum=1000))

        return respond({
            'project': {
                'id': project.id, 'name': project.name, 'code': project.code or '',
                'state': project.ethara_state, 'type': project.ethara_project_type,
                'gpm': project.gpm_id.display_name or '',
                'created': str(project.create_date or ''),
                'activated_at': str(project.activated_at or ''),
                'archived_at': str(project.archived_at or ''),
            },
            'totals': {
                'ever_allocated': len(allocations),
                'currently_allocated': len(allocations.filtered('is_open')),
                'in_onboarding': len(project.onboarding_ids.filtered(
                    lambda o: not o.unlocked)),
                'submissions': Entry.search_count([
                    ('project_id', '=', project_id), ('state', '=', 'submitted')]),
                'avg_days_to_productive': round(
                    sum(productive) / len(productive), 1) if productive else 0,
                'days_by_phase': _phase_breakdown(phases),
                'people_by_current_phase': by_phase_now,
            },
            'people': people,
            'content': {
                'knowledge': len(project.knowledge_doc_ids.filtered('active')),
                'training': len(project.training_ids.filtered('active')),
                'assessment': project.ethara_assessment_id.title or '',
                'stagelist_version': project.stagelist_template_id.version or 0,
                'feedback_version': project.feedback_template_id.version or 0,
            },
            'timeline': [_event_dict(e) for e in events],
        })

    # ------------------------------------------------------------------
    # the board a GPM opens first
    # ------------------------------------------------------------------
    @route('/api/project-os/analytics/overview', ['GET'], min_role='pl')
    def overview(self, ctx):
        params = request.params
        today = ctx.today()
        date_from = params.get('date_from') or str(
            fields.Date.subtract(fields.Date.to_date(today), days=30))
        date_to = params.get('date_to') or str(today)

        Entry = ctx.env['epo.form.entry'].sudo()
        base = [('state', '=', 'submitted'),
                ('employee_id', 'in', ctx.scope_employee_ids),
                ('business_date', '>=', date_from), ('business_date', '<=', date_to)]

        def by_project(form_type):
            grouped = Entry._read_group(
                base + [('form_type', '=', form_type)],
                groupby=['project_id'], aggregates=['__count'])
            return [{'project_id': p.id, 'project': p.name, 'count': c}
                    for p, c in grouped]

        Roster = ctx.env['epo.roster.day'].sudo()
        status_today = Roster._read_group(
            [('business_date', '=', today),
             ('employee_id', 'in', ctx.scope_employee_ids)],
            groupby=['tasking_status'], aggregates=['__count'])

        Allocation = ctx.env['epo.allocation'].sudo()
        open_allocations = Allocation.search([
            ('employee_id', 'in', ctx.scope_employee_ids), ('date_to', '=', False)])
        ramping = open_allocations.filtered(
            lambda a: a.current_phase in ('onboarding', 'sop', 'training', 'assessment'))

        # Only the pipeline's own projects: the registry is shared with the budget
        # side, and counting its rows here would make "projects in setup" mean the
        # number of projects nobody has started a delivery gate on.
        domain = [('is_project_os', '=', True)]
        if not ctx.is_manager:
            domain.append(('id', 'in', ctx.accessible_project_ids))
        projects = ctx.env['project.project'].sudo().search(domain)

        return respond({
            'window': {'from': date_from, 'to': date_to},
            'projects': {
                'total': len(projects),
                'active': len(projects.filtered(lambda p: p.ethara_state == 'active')),
                'setup': len(projects.filtered(lambda p: p.ethara_state == 'setup')),
                'archived': len(projects.filtered(lambda p: p.ethara_state == 'archived')),
                'blocked': [{
                    'id': p.id, 'name': p.name, 'missing': p.gate_blockers,
                } for p in projects.filtered(
                    lambda p: p.ethara_state == 'setup' and p.gate_blockers)],
            },
            'people': {
                'allocated': len(open_allocations),
                'ramping_up': len(ramping),
                'tasking': len(open_allocations) - len(ramping),
                'by_status_today': {status: count for status, count in status_today},
            },
            'submissions': {
                'stagelist_total': Entry.search_count(
                    base + [('form_type', '=', 'stagelist')]),
                'feedback_total': Entry.search_count(
                    base + [('form_type', '=', 'feedback')]),
                'stagelist_by_project': by_project('stagelist'),
                'feedback_by_project': by_project('feedback'),
            },
        })

    # ------------------------------------------------------------------
    @route('/api/project-os/timeline', ['GET'], min_role='pl')
    def timeline(self, ctx):
        """The raw stream, filtered. Useful for a live activity feed."""
        params = request.params
        domain = []
        if params.get('employee_id'):
            ctx.assert_can_see_employee(params['employee_id'])
            domain.append(('employee_id', '=', opt_int(params, 'employee_id')))
        else:
            domain.append('|')
            domain.append(('employee_id', 'in', ctx.scope_employee_ids))
            domain.append(('employee_id', '=', False))
        if params.get('project_id'):
            domain.append(('project_id', '=', opt_int(params, 'project_id')))
        if params.get('category'):
            domain.append(('category', '=', params['category']))
        if params.get('date_from'):
            domain.append(('business_date', '>=', params['date_from']))
        events = ctx.env['epo.timeline.event'].sudo().search(
            domain, limit=opt_int(params, 'limit', 100, minimum=1, maximum=500))
        return respond([_event_dict(e) for e in events])
