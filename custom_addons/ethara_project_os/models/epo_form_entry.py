"""The submission ledger — append-only.

Every number on every screen is a live aggregate over this table. There are no counter
columns anywhere in this module, which is the prototype's most consistently applied
decision and the one most worth not losing: counters drift, aggregates cannot.

An entry is evidence. Once submitted it cannot be edited or deleted — a correction is a
void plus a fresh submission, so a count that was true yesterday stays true.
"""

import json
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_TEXT = 65536


class EpoFormEntry(models.Model):
    _name = 'epo.form.entry'
    _description = 'Project OS Form Entry'
    _order = 'submitted_at desc, id desc'
    _rec_name = 'display_name'

    public_ref = fields.Char(
        required=True, copy=False, index=True, readonly=True,
        default=lambda s: str(uuid.uuid4()),
        help='Stable external reference. Safe to expose; the database id is not.')
    template_id = fields.Many2one(
        'epo.form.template', required=True, ondelete='restrict', index=True)
    project_id = fields.Many2one(
        'project.project', required=True, ondelete='restrict', index=True)
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True)
    pod_id = fields.Many2one(
        'epo.pod', related='employee_id.epo_pod_id', store=True, index=True)
    form_type = fields.Selection(
        [('stagelist', 'Stagelist'), ('feedback', 'Feedback Taker')],
        required=True, index=True, readonly=True)
    template_version = fields.Integer(
        required=True, readonly=True,
        help='Snapshot of the version actually filled. Without it, a later version bump '
             'silently re-labels historical answers.')
    business_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True)
    state = fields.Selection(
        [('draft', 'Draft'), ('submitted', 'Submitted'), ('void', 'Void')],
        required=True, default='draft', index=True)
    submitted_at = fields.Datetime(readonly=True, index=True)
    idempotency_key = fields.Char(
        copy=False,
        help='Client-supplied. A retried POST with the same key is a no-op instead of a '
             'duplicate row inflating every count.')

    value_ids = fields.One2many('epo.form.value', 'entry_id', string='Answers')

    void_reason = fields.Text()
    voided_by_id = fields.Many2one('res.users', ondelete='restrict')
    voided_at = fields.Datetime()

    _public_ref_uniq = models.Constraint(
        'UNIQUE (public_ref)', 'Duplicate public reference.')
    _idem_uniq = models.UniqueIndex(
        '(employee_id, idempotency_key) WHERE idempotency_key IS NOT NULL',
        'That submission was already recorded.')
    _one_draft = models.UniqueIndex(
        "(employee_id, template_id) WHERE state = 'draft'",
        'You already have a draft open on this form. Finish or discard it first.')
    _submit_stamped = models.Constraint(
        "CHECK (state <> 'submitted' OR submitted_at IS NOT NULL)",
        'A submitted entry must carry its submission timestamp.')
    _void_audited = models.Constraint(
        "CHECK (state <> 'void' OR (void_reason IS NOT NULL AND voided_by_id IS NOT NULL))",
        'Voiding an entry must record who did it and why.')

    @api.depends('employee_id', 'project_id', 'submitted_at')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s · %s' % (
                rec.employee_id.display_name or '?', rec.project_id.name or '?')

    # ------------------------------------------------------------------
    # the submission guard
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            template = self.env['epo.form.template'].browse(vals['template_id'])
            vals['project_id'] = template.project_id.id
            vals['form_type'] = template.form_type
            vals['template_version'] = template.version
        entries = super().create(vals_list)
        entries._assert_admissible()
        return entries

    def _assert_admissible(self):
        """Every rule the prototype checked in JavaScript — plus the three it did not.

        The one that matters most is allocation: without it a Tasker can post
        feedback against any project in the system just by knowing its template id."""
        for entry in self:
            template = entry.template_id
            if template.project_id != entry.project_id:
                raise ValidationError(_('That form does not belong to that project.'))
            if template.state != 'published':
                raise ValidationError(_(
                    'The %s form is not published yet.', template.form_type))
            if entry.project_id.ethara_state != 'active':
                raise ValidationError(_(
                    'Project "%(name)s" is %(state)s — submissions are closed.',
                    name=entry.project_id.name, state=entry.project_id.ethara_state))
            if not entry._is_allocated():
                raise ValidationError(_(
                    '%(name)s is not allocated to %(project)s on %(date)s.',
                    name=entry.employee_id.display_name,
                    project=entry.project_id.name, date=entry.business_date))
            if entry.form_type == 'stagelist':
                onboarding = self.env['epo.onboarding'].sudo()._get(
                    entry.employee_id.id, entry.project_id.id)
                if not onboarding.unlocked:
                    raise ValidationError(_(
                        'Onboarding is not complete for %(name)s on %(project)s — '
                        'still outstanding: %(missing)s.',
                        name=entry.employee_id.display_name,
                        project=entry.project_id.name,
                        missing=onboarding.blockers or _('SOP → Training → Assessment')))

    def _is_allocated(self):
        self.ensure_one()
        return bool(self.env['epo.allocation'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('project_id', '=', self.project_id.id),
            ('date_from', '<=', self.business_date),
            '|', ('date_to', '=', False), ('date_to', '>=', self.business_date),
        ], limit=1))

    # ------------------------------------------------------------------
    # append-only
    # ------------------------------------------------------------------
    def write(self, vals):
        frozen = {'template_id', 'project_id', 'employee_id', 'form_type',
                  'template_version', 'business_date', 'submitted_at'}
        for entry in self:
            if entry.state == 'submitted':
                if frozen & set(vals):
                    raise UserError(_(
                        'Submission %s is evidence and cannot be changed. Void it and '
                        'submit a correction instead.', entry.public_ref))
                if vals.get('state') not in (None, 'submitted', 'void'):
                    raise UserError(_(
                        'A submitted entry cannot go back to %s.', vals['state']))
        return super().write(vals)

    def unlink(self):
        if any(e.state != 'draft' for e in self):
            raise UserError(_(
                'Submitted entries are append-only. Void the entry instead of deleting it.'))
        return super().unlink()

    def action_submit(self):
        """Freeze the entry. Required-field completeness is checked here, not at insert
        time — a draft is legitimately incomplete."""
        for entry in self:
            if entry.state == 'submitted':
                continue
            entry._assert_admissible()
            missing = entry._missing_required()
            if missing:
                raise UserError(_(
                    'Still unanswered: %s.', ', '.join(missing)))
            entry.write({'state': 'submitted', 'submitted_at': fields.Datetime.now()})
            self.env['epo.timeline.event'].log(
                event_type='entry_submitted',
                employee_id=entry.employee_id.id, project_id=entry.project_id.id,
                business_date=entry.business_date,
                summary=_('%(type)s submitted',
                          type=dict(self._fields['form_type'].selection)[entry.form_type]),
                payload={'ref': entry.public_ref, 'version': entry.template_version},
                record=entry)
        return True

    def _missing_required(self):
        self.ensure_one()
        answered = set(self.value_ids.mapped('field_id').ids)
        return [
            f.name for f in self.template_id.field_ids.sorted(lambda f: (f.sequence, f.id))
            if f.is_required and f.field_type != 'section_header' and f.id not in answered
        ]

    def action_void(self):
        """Admin-only correction path. The row stays; only its contribution to the
        counts goes away, and the reason is recorded next to it."""
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_admin')):
            raise UserError(_('Only an Admin may void a submission.'))
        reason = self.env.context.get('epo_reason')
        if not reason:
            raise UserError(_('Voiding a submission needs a reason.'))
        for entry in self:
            entry.write({
                'state': 'void', 'void_reason': reason,
                'voided_by_id': self.env.user.id, 'voided_at': fields.Datetime.now(),
            })
            self.env['epo.audit.log'].record('entry_void', entry, reason)
            self.env['epo.timeline.event'].log(
                event_type='entry_voided', employee_id=entry.employee_id.id,
                project_id=entry.project_id.id, summary=_('Submission voided'),
                payload={'ref': entry.public_ref, 'reason': reason}, record=entry)
        return True

    # ------------------------------------------------------------------
    @api.model
    def submit_answers(self, template_id, employee_id, answers, idempotency_key=None,
                       business_date=None):
        """Create-and-submit in one transaction — what the API calls.

        The idempotency key makes a retried request return the original entry rather
        than a second one, which is the difference between a flaky network and a
        corrupted count."""
        if idempotency_key:
            existing = self.search([
                ('employee_id', '=', employee_id),
                ('idempotency_key', '=', idempotency_key)], limit=1)
            if existing:
                return existing
        entry = self.create({
            'template_id': template_id,
            'employee_id': employee_id,
            'idempotency_key': idempotency_key,
            'business_date': business_date or fields.Date.context_today(self),
        })
        entry._write_answers(answers or [])
        entry.action_submit()
        return entry

    def _write_answers(self, answers):
        """answers: [{field_id, text?, number?, json?, date?, attachment_id?}]"""
        self.ensure_one()
        Value = self.env['epo.form.value']
        by_field = {f.id: f for f in self.template_id.field_ids}
        for answer in answers:
            field = by_field.get(int(answer.get('field_id') or 0))
            if not field or field.field_type == 'section_header':
                continue
            vals = {
                'entry_id': self.id,
                'field_id': field.id,
                'value_text': answer.get('text'),
                'value_number': answer.get('number'),
                'value_date': answer.get('date'),
                'value_json': json.dumps(answer['json']) if answer.get('json') is not None else False,
                'attachment_id': answer.get('attachment_id'),
            }
            if not any(vals[k] for k in
                       ('value_text', 'value_number', 'value_date', 'value_json',
                        'attachment_id')):
                continue
            Value.create(vals)

    def to_dict(self, with_schema=False):
        self.ensure_one()
        answers = {}
        for value in self.value_ids:
            answers[value.field_id.id] = value._to_dict()
        payload = {
            'id': self.id,
            'ref': self.public_ref,
            'project_id': self.project_id.id,
            'project_name': self.project_id.name,
            'employee_id': self.employee_id.id,
            'employee_name': self.employee_id.display_name,
            'employee_email': self.employee_id.work_email or '',
            'form_type': self.form_type,
            'template_version': self.template_version,
            'business_date': str(self.business_date),
            'state': self.state,
            'submitted_at': str(self.submitted_at or ''),
            'answers': answers,
        }
        if with_schema:
            payload['template'] = self.template_id.schema()
        return payload


class EpoFormValue(models.Model):
    """One answer to one field.

    ``field_id`` is ``restrict`` on purpose — that single word is the difference
    between the prototype (where editing a form deleted every answer ever given) and a
    system of record.
    """
    _name = 'epo.form.value'
    _description = 'Project OS Form Answer'
    _order = 'entry_id, field_id'
    _rec_name = 'display_name'

    entry_id = fields.Many2one(
        'epo.form.entry', required=True, ondelete='cascade', index=True)
    field_id = fields.Many2one(
        'epo.form.field', required=True, ondelete='restrict', index=True)
    field_type = fields.Selection(related='field_id.field_type', store=True)

    value_text = fields.Text()
    value_number = fields.Float()
    value_date = fields.Date()
    value_json = fields.Text(help='Checkbox array, or grid mapping {row: column}.')
    attachment_id = fields.Many2one(
        'ir.attachment', ondelete='restrict',
        help='Image and file answers are attachments behind Odoo\'s ACL — never an '
             'inline data URL, which is how the prototype let a Tasker inject '
             'markup into a PM\'s browser.')

    _one_per_field = models.Constraint(
        'UNIQUE (entry_id, field_id)', 'That field already has an answer on this entry.')
    _text_bounded = models.Constraint(
        f'CHECK (value_text IS NULL OR length(value_text) <= {MAX_TEXT})',
        'That answer is too long.')

    @api.depends('field_id', 'value_text', 'value_number')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.field_id.name}: {rec._display_value()}'

    @api.constrains('field_id', 'value_text', 'value_number', 'value_json',
                    'attachment_id', 'value_date')
    def _check_matches_field(self):
        for value in self:
            field = value.field_id
            ftype = field.field_type
            if ftype == 'section_header':
                raise ValidationError(_(
                    'A section header cannot hold an answer.'))
            if ftype in ('image', 'file'):
                if not value.attachment_id:
                    raise ValidationError(_(
                        '"%s" expects an uploaded file.', field.name))
                if value.value_text:
                    raise ValidationError(_(
                        '"%s" is an upload field and must not carry inline text.',
                        field.name))
            if ftype == 'number' and not value.value_number and value.value_number != 0:
                raise ValidationError(_('"%s" expects a number.', field.name))
            if ftype == 'date' and not value.value_date:
                raise ValidationError(_('"%s" expects a date.', field.name))
            if ftype == 'email' and value.value_text and not EMAIL_RE.match(value.value_text):
                raise ValidationError(_('"%s" expects an email address.', field.name))
            if ftype in ('dropdown', 'radio') and value.value_text:
                if value.value_text not in field.options:
                    raise ValidationError(_(
                        '"%(value)s" is not one of the choices for "%(label)s".',
                        value=value.value_text, label=field.name))
            if ftype == 'checkboxes' and value.value_json:
                chosen = json.loads(value.value_json)
                extra = set(chosen or []) - set(field.options)
                if extra:
                    raise ValidationError(_(
                        'Selection outside the choices of "%(label)s": %(extra)s.',
                        label=field.name, extra=', '.join(sorted(extra))))
            if value.entry_id.template_id != field.template_id:
                raise ValidationError(_(
                    'That answer belongs to a field of a different form.'))

    @api.constrains('entry_id')
    def _check_entry_open(self):
        for value in self:
            if value.entry_id.state == 'submitted':
                raise ValidationError(_(
                    'Submission %s is frozen — answers cannot be added or changed.',
                    value.entry_id.public_ref))

    def _display_value(self):
        self.ensure_one()
        if self.value_text:
            return self.value_text
        if self.value_json:
            return self.value_json
        if self.value_date:
            return str(self.value_date)
        if self.attachment_id:
            return self.attachment_id.name
        return str(self.value_number or '')

    def _to_dict(self):
        self.ensure_one()
        parsed = None
        if self.value_json:
            try:
                parsed = json.loads(self.value_json)
            except ValueError:
                parsed = None
        return {
            'field_id': self.field_id.id,
            'label': self.field_id.name,
            'field_type': self.field_id.field_type,
            'text': self.value_text or None,
            'number': self.value_number if self.field_type == 'number' else None,
            'date': str(self.value_date) if self.value_date else None,
            'json': parsed,
            'attachment_id': self.attachment_id.id or None,
            'attachment_name': self.attachment_id.name or None,
        }
