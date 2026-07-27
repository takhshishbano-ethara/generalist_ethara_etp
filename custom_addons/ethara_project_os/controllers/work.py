"""The daily work API: roster, allocation, onboarding, filling and review.

Role shapes, end to end:

* **PM** — reads their own onboarding, their own form, submits entries.
* **PL** — reads and edits their pod's roster, reviews their pod's submissions.
* **GPM** — allocates people to projects, sees everything, org-wide counts.
* **Admin** — everything, plus the void and unlock paths.
"""

from odoo import http
from odoo.http import request

from .utils import need_int, opt_int, payload, respond, route


class ProjectOsWork(http.Controller):

    # ------------------------------------------------------------------
    # onboarding — the pod member's path to being allowed to task
    # ------------------------------------------------------------------
    @route('/api/project-os/me/onboarding', ['GET'])
    def my_onboarding(self, ctx):
        allocation = ctx.employee.epo_current_allocation_id
        if not allocation:
            return respond({'has_project': False},
                           message='You are not allocated to a project yet.')
        record = ctx.env['epo.onboarding'].sudo()._get(
            ctx.employee.id, allocation.project_id.id)
        return respond({'has_project': True, **record.to_dict()})

    @route('/api/project-os/me/onboarding/sop', ['POST'])
    def mark_sop(self, ctx):
        record = self._my_onboarding(ctx)
        if not record:
            return respond(message='You are not allocated to a project yet.', status=400)
        record.sudo().action_mark_sop()
        return respond(record.to_dict(), message='SOP marked as read.')

    @route('/api/project-os/me/onboarding/training', ['POST'])
    def mark_training(self, ctx):
        record = self._my_onboarding(ctx)
        if not record:
            return respond(message='You are not allocated to a project yet.', status=400)
        record.sudo().action_mark_training()
        return respond(record.to_dict(), message='Training marked as attended.')

    def _my_onboarding(self, ctx):
        allocation = ctx.employee.epo_current_allocation_id
        if not allocation:
            return None
        return ctx.env['epo.onboarding'].sudo()._get(
            ctx.employee.id, allocation.project_id.id)

    @route('/api/project-os/onboarding/<int:onboarding_id>/waive', ['POST'],
           min_role='gpm')
    def waive_onboarding(self, ctx, onboarding_id):
        """Let one person start on one project without finishing the gate.

        The escape hatch every real operation needs, and the one place where somebody
        begins work with no evidence they are ready — so the reason is mandatory and it
        lands in the audit log as well as the timeline."""
        body = payload()
        if not body.get('reason'):
            return respond(message='A waiver needs a reason.', status=400)
        record = ctx.env['epo.onboarding'].browse(onboarding_id).exists()
        if not record:
            return respond(message='No such onboarding record.', status=404)
        record.with_context(epo_reason=body['reason']).action_waive()
        return respond(record.to_dict(), message='Onboarding waived.')

    @route('/api/project-os/onboarding', ['GET'], min_role='pl')
    def onboarding_board(self, ctx):
        """Who is still ramping up, and on what. The Pod Lead's morning question."""
        domain = [('employee_id', 'in', ctx.scope_employee_ids)]
        if request.params.get('project_id'):
            domain.append(('project_id', '=', int(request.params['project_id'])))
        if request.params.get('pending_only', '1') in ('1', 'true', 'True'):
            domain.append(('unlocked', '=', False))
        records = ctx.env['epo.onboarding'].sudo().search(domain)
        return respond([{
            'id': r.id,
            'employee_id': r.employee_id.id,
            'employee_name': r.employee_id.display_name,
            'pod': r.pod_id.display_name or '',
            'project_id': r.project_id.id,
            'project_name': r.project_id.name,
            'sop_done': r.sop_done,
            'training_done': r.training_done,
            'assessment_passed': r.assessment_passed,
            'assessment_attempts': r.assessment_attempts,
            'unlocked': r.unlocked,
            'blockers': r.blockers or '',
            'waived': bool(r.waived_by_id),
        } for r in records])

    # ------------------------------------------------------------------
    # assessment — taken here, graded over there
    # ------------------------------------------------------------------
    @route('/api/project-os/me/assessment', ['GET'])
    def get_assessment(self, ctx):
        """Where to go and sit it. Odoo hosts nothing — v2 §4.6.2 makes this a link.

        Gated behind the SOP and the training exactly as before, so nobody is sent to
        the paper before they have read what it is about.
        """
        record = self._my_onboarding(ctx)
        if not record:
            return respond(message='You are not allocated to a project yet.', status=400)
        assessment = record.project_id.ethara_assessment_id
        if not assessment:
            return respond({'has_assessment': False},
                           message='This project has no assessment.')
        if not record.sop_done or (record._training_required() and not record.training_done):
            return respond(
                message='Complete the SOP and the training before the assessment.',
                status=403)
        return respond({
            'has_assessment': True,
            **assessment.to_dict(),
            'passed': record.assessment_passed,
            'verified_by': record.assessment_verified_by.name or '',
            'verified_at': str(record.assessment_verified_at or ''),
            # Odoo cannot tell you if you passed — somebody has to read the result
            # over there and say so.
            'awaiting_verification': not record.assessment_passed,
        })


    @route('/api/project-os/onboarding/<int:onboarding_id>/verify-assessment',
           ['POST'], min_role='pl')
    def verify_assessment(self, ctx, onboarding_id):
        """Record that somebody read the external result and it passed (v2 §4.6.3).

        Pod Lead or above. Optionally carries the ``score`` the verifier saw — the
        project's minimum score is checked against it when this person is allocated, so
        omitting it means they can never clear a bar.
        """
        body = payload()
        record = ctx.env['epo.onboarding'].browse(onboarding_id).exists()
        if not record:
            return respond(message='No such onboarding record.', status=404)
        score = body.get('score')
        if score not in (None, ''):
            try:
                score = float(score)
            except (TypeError, ValueError):
                return respond(message='score must be a number.', status=400)
            if not 0 <= score <= 100:
                return respond(message='score is a percentage, 0-100.', status=400)
        else:
            score = None
        record.with_context(epo_score=score).action_verify_assessment()
        return respond(record.to_dict(), message='Assessment verified.')


    # ------------------------------------------------------------------
    # filling — stagelist and feedback
    # ------------------------------------------------------------------
    @route('/api/project-os/me/form', ['GET'])
    def my_form(self, ctx):
        """The form to fill right now, resolved from today's roster.

        Returns a reason rather than an error when there is nothing to fill: "no
        project" and "no published form" are ordinary states a client renders, not
        failures."""
        form_type = request.params.get('form_type') or 'stagelist'
        if form_type not in ('stagelist', 'feedback'):
            form_type = 'stagelist'
        allocation = ctx.employee.epo_current_allocation_id
        if not allocation:
            return respond({'has_form': False, 'reason': 'no_project',
                            'form_type': form_type})
        project = allocation.project_id
        template = (project.stagelist_template_id if form_type == 'stagelist'
                    else project.feedback_template_id)
        if not template:
            return respond({'has_form': False, 'reason': 'no_form',
                            'form_type': form_type, 'project_name': project.name})
        if form_type == 'stagelist':
            onboarding = ctx.env['epo.onboarding'].sudo()._get(
                ctx.employee.id, project.id)
            if not onboarding.unlocked:
                return respond({
                    'has_form': False, 'reason': 'onboarding_incomplete',
                    'form_type': form_type, 'blockers': onboarding.blockers,
                })
        my_count = ctx.env['epo.form.entry'].sudo().search_count([
            ('employee_id', '=', ctx.employee.id), ('form_type', '=', form_type),
            ('project_id', '=', project.id), ('state', '=', 'submitted')])
        return respond({
            'has_form': True,
            'form_type': form_type,
            'template': template.schema(),
            'my_count': my_count,
        })

    @route('/api/project-os/entries', ['POST'])
    def submit_entry(self, ctx):
        """Submit one filled form.

        ``idempotency_key`` is honoured: a retried POST returns the original entry
        instead of inflating every count with a duplicate."""
        body = payload()
        template_id = need_int(body, 'template_id')
        employee_id = ctx.employee.id
        on_behalf_of = opt_int(body, 'employee_id')
        if on_behalf_of and on_behalf_of != employee_id:
            # Filling on someone else's behalf is a lead action, and it is recorded
            # against the person the work belongs to.
            ctx.assert_can_see_employee(on_behalf_of)
            employee_id = on_behalf_of
        entry = ctx.env['epo.form.entry'].sudo().submit_answers(
            template_id=template_id,
            employee_id=employee_id,
            answers=body.get('answers') or [],
            idempotency_key=body.get('idempotency_key'),
            business_date=body.get('business_date'),
        )
        return respond({'entry_id': entry.id, 'ref': entry.public_ref,
                        'state': entry.state}, message='Submitted.')

    # ------------------------------------------------------------------
    # review
    # ------------------------------------------------------------------
    @route('/api/project-os/submissions', ['GET'])
    def submissions(self, ctx):
        params = request.params
        domain = [('state', '=', 'submitted'),
                  ('employee_id', 'in', ctx.scope_employee_ids)]
        if params.get('form_type'):
            domain.append(('form_type', '=', params['form_type']))
        if params.get('project_id'):
            domain.append(('project_id', '=', need_int(params, 'project_id')))
        if params.get('date_from'):
            domain.append(('business_date', '>=', params['date_from']))
        if params.get('date_to'):
            domain.append(('business_date', '<=', params['date_to']))
        entries = ctx.env['epo.form.entry'].sudo().search(
            domain,
            limit=opt_int(params, 'limit', 200, minimum=1, maximum=500),
            offset=opt_int(params, 'offset', 0, minimum=0))
        return respond([{
            'id': e.id, 'ref': e.public_ref,
            'employee_id': e.employee_id.id,
            'employee_name': e.employee_id.display_name,
            'employee_email': e.employee_id.work_email or '',
            'pod': e.pod_id.display_name or '',
            'project_id': e.project_id.id, 'project_name': e.project_id.name,
            'form_type': e.form_type, 'business_date': str(e.business_date),
            'submitted_at': str(e.submitted_at or ''),
        } for e in entries])

    @route('/api/project-os/submissions/<int:entry_id>', ['GET'])
    def submission_detail(self, ctx, entry_id):
        entry = ctx.env['epo.form.entry'].sudo().browse(entry_id).exists()
        if not entry:
            return respond(message='No such submission.', status=404)
        ctx.assert_can_see_employee(entry.employee_id.id)
        return respond(entry.to_dict(with_schema=True))

    @route('/api/project-os/submissions/<int:entry_id>/void', ['POST'], min_role='admin')
    def void_submission(self, ctx, entry_id):
        body = payload()
        if not body.get('reason'):
            return respond(message='Voiding a submission needs a reason.', status=400)
        entry = ctx.env['epo.form.entry'].browse(entry_id).exists()
        if not entry:
            return respond(message='No such submission.', status=404)
        entry.with_context(epo_reason=body['reason']).action_void()
        return respond(message='Voided.')

    @route('/api/project-os/counts', ['GET'])
    def counts(self, ctx):
        """Submission counts, scoped by role. The single ground truth behind every
        number the UI shows — computed, never stored."""
        params = request.params
        form_type = params.get('form_type') or 'stagelist'
        domain = [('state', '=', 'submitted'), ('form_type', '=', form_type),
                  ('employee_id', 'in', ctx.scope_employee_ids)]
        if params.get('date_from'):
            domain.append(('business_date', '>=', params['date_from']))
        if params.get('date_to'):
            domain.append(('business_date', '<=', params['date_to']))
        grouped = ctx.env['epo.form.entry'].sudo()._read_group(
            domain, groupby=['project_id'], aggregates=['__count'])
        rows = [{'project_id': project.id, 'project': project.name, 'count': count}
                for project, count in grouped]
        rows.sort(key=lambda r: r['count'], reverse=True)
        return respond({
            'form_type': form_type,
            'total': sum(r['count'] for r in rows),
            'by_project': rows,
        })

    # ------------------------------------------------------------------
    # roster — the Pod Lead's board
    # ------------------------------------------------------------------
    @route('/api/project-os/roster', ['GET'], min_role='pl')
    def roster(self, ctx):
        """Today's board for everyone in scope, including people with no row yet —
        an empty roster line is exactly what the lead needs to see and fix."""
        params = request.params
        day = params.get('date') or str(ctx.today())
        employee_ids = ctx.scope_employee_ids
        if params.get('pod_id'):
            pod_members = ctx.env['hr.employee'].sudo().search([
                ('epo_pod_id', '=', need_int(params, 'pod_id')),
                ('id', 'in', employee_ids)])
            employee_ids = pod_members.ids
        employees = ctx.env['hr.employee'].sudo().browse(employee_ids).filtered('active')
        rows = ctx.env['epo.roster.day'].sudo().search([
            ('employee_id', 'in', employees.ids), ('business_date', '=', day)])
        by_employee = {r.employee_id.id: r for r in rows}
        payload_rows = []
        for employee in employees:
            row = by_employee.get(employee.id)
            allocation = employee.epo_current_allocation_id
            payload_rows.append({
                'employee_id': employee.id,
                'name': employee.display_name,
                'email': employee.work_email or '',
                'seat': employee.epo_seat_no or '',
                'pod': employee.epo_pod_id.display_name or '',
                'pod_lead': employee.epo_pod_lead_id.display_name or '',
                'role': employee.epo_role or '',
                'roster_id': row.id if row else None,
                'project_id': (row.project_id.id if row else allocation.project_id.id) or None,
                'project': (row.project_id.name if row else allocation.project_id.name) or '',
                'tasking_status': row.tasking_status if row else None,
                'present': row.present if row else False,
                'issue': (row.issue or '') if row else '',
                'locked': row.locked if row else False,
                'onboarding_unlocked': bool(
                    allocation and allocation.onboarding_id.unlocked),
                'current_phase': allocation.current_phase or '' if allocation else '',
            })
        return respond({'date': day, 'rows': payload_rows})

    @route('/api/project-os/roster/<int:employee_id>', ['PATCH', 'POST'], min_role='pl')
    def set_roster(self, ctx, employee_id):
        """Set one person's day. Creates the row if it does not exist yet."""
        ctx.assert_can_see_employee(employee_id)
        body = payload()
        day = body.get('date') or ctx.today()
        values = {}
        for key in ('tasking_status', 'present', 'issue'):
            if key in body:
                values[key] = body[key]
        if 'project_id' in body:
            values['project_id'] = opt_int(body, 'project_id') or False
        row = ctx.env['epo.roster.day'].sudo().upsert(employee_id, day, values)
        return respond({
            'roster_id': row.id, 'employee_id': employee_id,
            'date': str(row.business_date), 'tasking_status': row.tasking_status,
            'project_id': row.project_id.id or None, 'present': row.present,
        }, message='Saved.')

    @route('/api/project-os/roster/<int:roster_id>/unlock', ['POST'], min_role='admin')
    def unlock_roster(self, ctx, roster_id):
        body = payload()
        if not body.get('reason'):
            return respond(message='Unlocking a closed day needs a reason.', status=400)
        row = ctx.env['epo.roster.day'].browse(roster_id).exists()
        if not row:
            return respond(message='No such roster row.', status=404)
        row.with_context(epo_reason=body['reason']).action_unlock()
        return respond(message='Unlocked.')

    # ------------------------------------------------------------------
    # allocation — the GPM's staffing action
    # ------------------------------------------------------------------
    @route('/api/project-os/allocations', ['GET'], min_role='pl')
    def allocations(self, ctx):
        params = request.params
        domain = [('employee_id', 'in', ctx.scope_employee_ids)]
        if params.get('project_id'):
            domain.append(('project_id', '=', need_int(params, 'project_id')))
        if params.get('open_only', '1') in ('1', 'true', 'True'):
            domain.append(('date_to', '=', False))
        allocations = ctx.env['epo.allocation'].sudo().search(domain)
        return respond([_allocation_dict(a) for a in allocations])

    @route('/api/project-os/projects/<int:project_id>/candidates', ['GET'],
           min_role='gpm')
    def candidates(self, ctx, project_id):
        """The staffing shortlist: taskers at or above this project's minimum score.

        The GPM sets the bar on the project first; this returns only the people who
        clear it, each with the five facts a staffing call turns on — **name, seat,
        score, current project, leave**.

        ``?include_below_bar=1`` returns the rest as well, each carrying the reason it
        was cut. ``considered`` is always reported next to ``eligible`` so an empty
        shortlist reads as "nobody clears 80" rather than "the filter is broken" — the
        one thing a silent filter can never distinguish.

        ``?min_score=`` previews a different bar without saving it, so a GPM can see
        what lowering it to 70 would actually buy before committing.
        """
        project = ctx.env['project.project'].browse(project_id).exists()
        if not project or not project.is_project_os:
            return respond(message='No such project.', status=404)

        params = request.params
        include_below = str(params.get('include_below_bar', '')) in ('1', 'true', 'True')
        # A preview never writes: the bar itself is the GPM's decision, made on the
        # project record, not a query string.
        bar = None
        preview = params.get('min_score')
        if preview not in (None, ''):
            try:
                bar = float(preview)
            except (TypeError, ValueError):
                return respond(message='min_score must be a number.', status=400)
            if not 0 <= bar <= 100:
                return respond(message='min_score is a percentage, 0-100.', status=400)

        rows = project.candidates(include_below_bar=include_below, min_score=bar)
        eligible = [r for r in rows if r['eligible']]
        return respond({
            'project_id': project.id,
            'project_name': project.name,
            'min_assessment_score': (
                project.min_assessment_score if bar is None else bar),
            'is_preview': bar is not None,
            'eligible_count': len(eligible),
            'available_count': len([r for r in eligible if not r['on_leave']]),
            # Everybody weighed, whether or not they came back in `candidates` — this
            # is what makes an empty shortlist readable.
            'considered_count': len(project._candidate_pool()),
            'candidates': rows,
        })

    @route('/api/project-os/allocations', ['POST'], min_role='gpm')
    def allocate(self, ctx):
        body = payload()
        allocation = ctx.env['epo.allocation'].create({
            'project_id': need_int(body, 'project_id'),
            'employee_id': need_int(body, 'employee_id'),
            'date_from': body.get('date_from') or ctx.today(),
            'allocation_pct': body.get('allocation_pct', 100.0),
            'role_on_project': body.get('role_on_project') or 'pm',
            'note': body.get('note') or False,
            # Below the project's minimum score is allowed, but only deliberately:
            # somebody has to be first on a project, and a new joiner has no score.
            'override_reason': body.get('override_reason') or False,
        })
        return respond(_allocation_dict(allocation), message='Allocated.')

    @route('/api/project-os/allocations/bulk', ['POST'], min_role='gpm')
    def allocate_bulk(self, ctx):
        body = payload()
        project_id = need_int(body, 'project_id')
        employee_ids = [int(i) for i in (body.get('employee_ids') or [])]
        if not employee_ids:
            return respond(message='Select at least one person.', status=400)
        created, skipped = ctx.env['epo.allocation'].allocate_many(
            project_id=project_id,
            employee_ids=employee_ids,
            date_from=body.get('date_from'),
            allocation_pct=body.get('allocation_pct', 100.0),
            role_on_project=body.get('role_on_project') or 'pm',
            override_reason=body.get('override_reason') or None,
        )
        return respond({
            'allocated': len(created),
            'skipped': skipped,
            'project': ctx.env['project.project'].browse(project_id).name,
        }, message=f'Allocated {len(created)} people.'
                   + (f' {len(skipped)} were already on it.' if skipped else ''))

    @route('/api/project-os/allocations/<int:allocation_id>/release', ['POST'],
           min_role='gpm')
    def release(self, ctx, allocation_id):
        allocation = ctx.env['epo.allocation'].browse(allocation_id).exists()
        if not allocation:
            return respond(message='No such allocation.', status=404)
        body = payload()
        if body.get('date_to'):
            allocation.write({'date_to': body['date_to']})
        else:
            allocation.action_release()
        return respond(_allocation_dict(allocation), message='Released.')


def _allocation_dict(allocation):
    return {
        'id': allocation.id,
        'employee_id': allocation.employee_id.id,
        'employee_name': allocation.employee_id.display_name,
        'pod': allocation.pod_id.display_name or '',
        'project_id': allocation.project_id.id,
        'project_name': allocation.project_id.name,
        'role_on_project': allocation.role_on_project,
        'date_from': str(allocation.date_from),
        'date_to': str(allocation.date_to or ''),
        'allocated_by': allocation.allocated_by_id.name or '',
        'released_by': allocation.released_by_id.name or '',
        'min_score_applied': allocation.min_score_applied,
        'score_at_allocation': allocation.score_at_allocation,
        'override_reason': allocation.override_reason or '',
        'is_open': allocation.is_open,
        'allocation_pct': allocation.allocation_pct,
        'current_phase': allocation.current_phase or '',
        'days_total': allocation.days_total,
        'days_onboarding': allocation.days_onboarding,
        'days_training': allocation.days_training,
        'days_assessment': allocation.days_assessment,
        'days_tasking': allocation.days_tasking,
        'days_to_productive': allocation.days_to_productive,
        'submission_count': allocation.submission_count,
        'onboarding_unlocked': allocation.onboarding_id.unlocked,
    }
