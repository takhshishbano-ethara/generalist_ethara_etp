"""The unified history — one append-only stream, two readings.

Read it filtered by ``employee_id`` and it is *a person's full history*: every project
they joined, every phase they moved through, every assessment attempt, every role grant,
the day they were benched.

Read it filtered by ``project_id`` and it is *the project's full history*: who joined
when, who was in onboarding while who was tasking, when the stagelist was republished,
when it went live.

Both readings come from the same rows, so they can never disagree — which is the whole
reason this is one model and not two audit tables.

Nothing writes here directly except ``log()``. Nothing edits or deletes a row: the
timeline is evidence.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

EVENT_TYPES = [
    # people
    ('role_granted', 'Role granted'),
    ('role_revoked', 'Role revoked'),
    ('pod_changed', 'Pod changed'),
    # project lifecycle
    ('project_created', 'Project created'),
    ('project_activated', 'Project activated'),
    ('project_archived', 'Project archived'),
    ('project_reopened', 'Project reopened'),
    ('knowledge_added', 'Knowledge added'),
    ('knowledge_removed', 'Knowledge removed'),
    ('training_set', 'Training set'),
    ('assessment_sent', 'SOP + training sent to assessment'),
    ('assessment_linked', 'Assessment linked'),
    ('form_published', 'Form published'),
    ('form_versioned', 'New form version'),
    # membership
    ('allocated', 'Allocated to project'),
    ('released', 'Released from project'),
    ('phase_started', 'Phase started'),
    ('phase_ended', 'Phase ended'),
    # onboarding
    ('sop_read', 'SOP marked read'),
    ('training_done', 'Training attended'),
    ('assessment_attempt', 'Assessment attempt'),
    ('assessment_passed', 'Assessment passed'),
    ('assessment_failed', 'Assessment failed'),
    ('onboarding_unlocked', 'Onboarding complete'),
    ('onboarding_waived', 'Onboarding waived'),
    # daily work
    ('roster_set', 'Roster updated'),
    ('entry_submitted', 'Entry submitted'),
    ('entry_voided', 'Entry voided'),
]


class EpoTimelineEvent(models.Model):
    _name = 'epo.timeline.event'
    _description = 'Project OS Timeline Event'
    _order = 'occurred_at desc, id desc'
    _rec_name = 'summary'

    occurred_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    business_date = fields.Date(index=True, default=fields.Date.context_today)
    event_type = fields.Selection(EVENT_TYPES, required=True, index=True)
    category = fields.Selection(
        [('people', 'People'), ('project', 'Project'), ('membership', 'Membership'),
         ('onboarding', 'Onboarding'), ('work', 'Work')],
        compute='_compute_category', store=True, index=True)

    employee_id = fields.Many2one('hr.employee', ondelete='restrict', index=True)
    project_id = fields.Many2one('project.project', ondelete='restrict', index=True)
    actor_id = fields.Many2one(
        'res.users', string='Done by', ondelete='restrict', default=lambda s: s.env.user)

    summary = fields.Char(required=True)
    payload_json = fields.Text(help='Machine-readable detail. Never edited.')
    res_model = fields.Char(index=True)
    res_id = fields.Integer(index=True)

    _CATEGORY_OF = {
        'role_granted': 'people', 'role_revoked': 'people', 'pod_changed': 'people',
        'project_created': 'project', 'project_activated': 'project',
        'project_archived': 'project', 'project_reopened': 'project',
        'knowledge_added': 'project', 'knowledge_removed': 'project',
        'training_set': 'project', 'assessment_sent': 'project',
        'assessment_linked': 'project',
        'form_published': 'project', 'form_versioned': 'project',
        'allocated': 'membership', 'released': 'membership',
        'phase_started': 'membership', 'phase_ended': 'membership',
        'sop_read': 'onboarding', 'training_done': 'onboarding',
        'assessment_attempt': 'onboarding', 'assessment_passed': 'onboarding',
        'assessment_failed': 'onboarding', 'onboarding_unlocked': 'onboarding',
        'onboarding_waived': 'onboarding',
        'roster_set': 'work', 'entry_submitted': 'work', 'entry_voided': 'work',
    }

    @api.depends('event_type')
    def _compute_category(self):
        for rec in self:
            rec.category = self._CATEGORY_OF.get(rec.event_type, 'work')

    @api.model
    def log(self, event_type, summary, employee_id=None, project_id=None,
            payload=None, record=None, business_date=None):
        """The only supported way to add to the timeline.

        Call it with sudo() from anywhere; it deliberately bypasses record rules on
        write (a Tasker's own action must be recorded even though a Tasker cannot read the
        whole stream) while the rules still govern who may *read* it back."""
        vals = {
            'event_type': event_type,
            'summary': summary,
            'employee_id': employee_id,
            'project_id': project_id,
            'payload_json': json.dumps(payload, default=str) if payload else False,
            'business_date': business_date or fields.Date.context_today(self),
        }
        if record is not None and record:
            vals['res_model'] = record._name
            vals['res_id'] = record.id
        return self.sudo().create(vals)

    def write(self, vals):
        raise UserError(_('The timeline is append-only — events cannot be edited.'))

    def unlink(self):
        raise UserError(_('The timeline is append-only — events cannot be deleted.'))
