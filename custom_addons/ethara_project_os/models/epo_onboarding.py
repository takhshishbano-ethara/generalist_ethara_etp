"""The onboarding gate — SOP → Training → Assessment, per person per project.

Two rules carried over from the prototype, both deliberate:

* **A stage with no content auto-passes.** A project with no mandatory training and no
  assessment gates on the SOP click alone. The gate must never block on a stage the GPM
  never set up — that is how a whole pod ends up unable to start work on a Monday.
* **Assessment completion is asserted by a human** (data model v2 §4.6.3). The paper is
  sat in an external application that Odoo never talks to, so nobody here can grade it.
  A PM or Pod Lead reads the external result and ticks it. That makes this the one place
  in the module where readiness is claimed rather than evidenced — so the toggle is
  group-restricted, stamped with who and when, and posted to the chatter (§11.2).

Unlocking is per project, not per person: a veteran joining a new project has still not
read *that* project's SOP.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EpoOnboarding(models.Model):
    _name = 'epo.onboarding'
    _description = 'Project OS Onboarding'
    _order = 'project_id, employee_id'
    _rec_name = 'display_name'
    # v2 §11.2 — the manual assessment verdict has to leave a trail, and chatter is
    # where it goes: who ticked it, when, and what score they said they saw.
    _inherit = ['mail.thread']

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True)
    project_id = fields.Many2one(
        'project.project', required=True, ondelete='cascade', index=True,
        domain=[('is_project_os', '=', True)])
    pod_id = fields.Many2one(
        'epo.pod', related='employee_id.epo_pod_id', store=True, index=True)

    sop_done = fields.Boolean(default=False, tracking=True)
    sop_done_at = fields.Datetime(readonly=True)
    training_done = fields.Boolean(default=False, tracking=True)
    training_done_at = fields.Datetime(readonly=True)

    # --- the manual assessment verdict (v2 §4.6.3) -----------------------
    # Stored and written by hand, not computed: Odoo never sees the paper, so there is
    # nothing to derive it from. Restricted to Pod Lead and above by action_verify().
    assessment_passed = fields.Boolean(
        default=False, tracking=True, string='Assessment passed',
        help='Ticked by a PM or Pod Lead after reading the result in the external '
             'assessment application. Odoo does not grade, so this is an assertion — '
             'which is why it records who made it and when.')
    assessment_verified_by = fields.Many2one(
        'res.users', string='Verified by', readonly=True, ondelete='restrict')
    assessment_verified_at = fields.Datetime(string='Verified at', readonly=True)
    # NOT in v2 §4.6.3. Kept because project.min_assessment_score is enforced on
    # allocation and this is the only thing left that can feed it — without it the
    # staffing bar has no input and candidates() can never rank anybody. The verifier is
    # already looking at a score when they tick the box; recording it costs one column.
    assessment_score = fields.Float(
        string='Score seen', tracking=True,
        help='The percentage the verifier read in the external application. Optional, '
             'but it is what the project minimum score is checked against when '
             'somebody is allocated — leave it blank and this person cannot clear a bar.')

    unlocked = fields.Boolean(
        compute='_compute_unlocked', store=True, index=True,
        help='True when every mandatory stage of this project is complete.')
    unlocked_at = fields.Datetime(readonly=True)
    blockers = fields.Char(compute='_compute_blockers', string='Outstanding')

    waived_by_id = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    waived_at = fields.Datetime(readonly=True)
    waiver_reason = fields.Text(readonly=True)

    _uniq = models.Constraint(
        'UNIQUE (employee_id, project_id)',
        'That person already has an onboarding record for this project.')
    _sop_stamped = models.Constraint(
        'CHECK (sop_done = false OR sop_done_at IS NOT NULL)',
        'A completed SOP step must carry its timestamp.')
    _training_stamped = models.Constraint(
        'CHECK (training_done = false OR training_done_at IS NOT NULL)',
        'A completed training step must carry its timestamp.')
    _verdict_stamped = models.Constraint(
        'CHECK (assessment_passed = false OR assessment_verified_by IS NOT NULL)',
        'A passed assessment must record who verified it.')
    _score_bounds = models.Constraint(
        'CHECK (assessment_score >= 0 AND assessment_score <= 100)',
        'A score is a percentage between 0 and 100.')
    _waiver_audited = models.Constraint(
        'CHECK (waived_by_id IS NULL OR waiver_reason IS NOT NULL)',
        'A waiver must record why it was granted.')

    @api.depends('employee_id', 'project_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s · %s' % (
                rec.employee_id.display_name or '?', rec.project_id.name or '?')

    def _missing_steps(self):
        """The stages still outstanding. A stage with no content auto-passes: the gate
        must never block on something the GPM never set up."""
        self.ensure_one()
        if self.waived_by_id:
            return []
        missing = []
        if not self.sop_done:
            missing.append(_('SOP'))
        if self._training_required() and not self.training_done:
            missing.append(_('Training'))
        if self._assessment_required() and not self.assessment_passed:
            missing.append(_('Assessment'))
        return missing

    @api.depends('sop_done', 'training_done', 'assessment_passed', 'waived_by_id',
                 'project_id.has_training', 'project_id.has_assessment')
    def _compute_unlocked(self):
        for rec in self:
            rec.unlocked = not rec._missing_steps()

    @api.depends('sop_done', 'training_done', 'assessment_passed', 'waived_by_id',
                 'project_id.has_training', 'project_id.has_assessment')
    def _compute_blockers(self):
        for rec in self:
            rec.blockers = ' → '.join(rec._missing_steps())

    def _training_required(self):
        self.ensure_one()
        return bool(self.project_id.training_ids.filtered(
            lambda t: t.active and t.is_mandatory))

    def _assessment_required(self):
        self.ensure_one()
        return bool(self.project_id.ethara_assessment_ids.filtered(
            lambda a: a.active and a.is_mandatory))

    # ------------------------------------------------------------------
    def write(self, vals):
        now = fields.Datetime.now()
        if vals.get('sop_done'):
            vals.setdefault('sop_done_at', now)
        if vals.get('training_done'):
            vals.setdefault('training_done_at', now)
        was_unlocked = {rec.id: rec.unlocked for rec in self}
        res = super().write(vals)
        newly = self.filtered(lambda r: r.unlocked and not was_unlocked.get(r.id))
        if newly:
            to_stamp = newly.filtered(lambda r: not r.unlocked_at)
            if to_stamp:
                # super() rather than write() — stamping must not re-enter this hook.
                super(EpoOnboarding, to_stamp.sudo()).write({'unlocked_at': now})
            newly._on_unlock()
        return res

    def _on_unlock(self):
        """Completing onboarding moves the allocation from onboarding into tasking, so
        the phase log and the roster agree without anyone doing it by hand."""
        for rec in self:
            self.env['epo.timeline.event'].log(
                event_type='onboarding_unlocked',
                employee_id=rec.employee_id.id, project_id=rec.project_id.id,
                summary=_('Cleared to task on %s', rec.project_id.name), record=rec)
            alloc = self.env['epo.allocation'].sudo().search([
                ('employee_id', '=', rec.employee_id.id),
                ('project_id', '=', rec.project_id.id),
                ('date_to', '=', False)], limit=1)
            if alloc:
                self.env['epo.allocation.phase'].sudo()._transition(
                    alloc, 'tasking', fields.Date.context_today(rec))
            row = self.env['epo.roster.day'].sudo().search([
                ('employee_id', '=', rec.employee_id.id),
                ('business_date', '=', fields.Date.context_today(rec))], limit=1)
            if row and row.tasking_status in ('onboarding', 'training', 'assessment') \
                    and not row.locked:
                row.write({'tasking_status': 'tasking'})

    # ------------------------------------------------------------------
    # the API surface
    # ------------------------------------------------------------------
    @api.model
    def _get(self, employee_id, project_id):
        """Fetch or create the record. Creating on read is safe here — the row is a
        checklist, and its existence carries no claim."""
        record = self.search([
            ('employee_id', '=', employee_id), ('project_id', '=', project_id)], limit=1)
        if not record:
            record = self.create({'employee_id': employee_id, 'project_id': project_id})
        return record

    @api.model
    def _refresh(self, employee_id, project_id):
        """Re-evaluate the gate after something outside this record changed it — a
        mandatory training being added, an assessment being linked or unlinked.

        ``unlocked`` is stored and its dependencies do not all live on this record, so
        invalidating is not enough: for a stored field that just re-reads the stale
        column. It has to be recomputed and flushed explicitly.

        Without this, a project that drops its last mandatory assessment could leave
        somebody locked out of the stagelist indefinitely.
        """
        record = self._get(employee_id, project_id)
        record.invalidate_recordset(['unlocked', 'blockers'])
        record._compute_unlocked()
        record.flush_recordset(['unlocked'])
        if record.unlocked and not record.unlocked_at:
            super(EpoOnboarding, record.sudo()).write(
                {'unlocked_at': fields.Datetime.now()})
            record._on_unlock()
        return record

    def action_verify_assessment(self):
        """Record that somebody read the external result and it passed (v2 §4.6.3).

        The one place in this module where readiness is asserted instead of evidenced,
        so it is deliberately noisy: Pod Lead or above only, stamped with who and when,
        and posted to the chatter with the score they said they saw. Pass the score in
        the context as ``epo_score``; the project's minimum is checked against it later,
        at allocation.
        """
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_pod_lead')):
            raise UserError(_(
                'Only a Pod Lead, GPM or Admin may verify an assessment result.'))
        # Context wins (that is the API path); otherwise take whatever the verifier
        # typed into assessment_score on the form, so the header button needs no wizard.
        ctx_score = self.env.context.get('epo_score')
        now = fields.Datetime.now()
        for rec in self:
            score = ctx_score if ctx_score is not None else (rec.assessment_score or None)
            values = {
                'assessment_passed': True,
                'assessment_verified_by': self.env.user.id,
                'assessment_verified_at': now,
            }
            if score is not None:
                values['assessment_score'] = float(score)
            rec.write(values)
            rec.message_post(body=_(
                'External assessment verified as passed by %(user)s%(score)s.',
                user=self.env.user.name,
                score=_(' — score seen: %.0f%%') % float(score) if score is not None
                else _(' (no score recorded)')))
            self.env['epo.timeline.event'].log(
                event_type='assessment_passed',
                employee_id=rec.employee_id.id, project_id=rec.project_id.id,
                summary=_('Assessment verified by %s', self.env.user.name),
                payload={'verified_by': self.env.user.name,
                         'score': rec.assessment_score}, record=rec)
        return True

    def action_unverify_assessment(self):
        """Undo a verification — a wrong tick has to be correctable, and clearing it
        must close the gate again rather than leave somebody tasking on a mistake."""
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_manager')):
            raise UserError(_('Only a GPM or Admin may withdraw a verification.'))
        for rec in self:
            rec.write({'assessment_passed': False,
                       'assessment_verified_by': False,
                       'assessment_verified_at': False})
            rec.message_post(body=_(
                'Assessment verification withdrawn by %s.', self.env.user.name))
        return True

    def action_mark_sop(self):
        self.write({'sop_done': True})
        for rec in self:
            self.env['epo.timeline.event'].log(
                event_type='sop_read', employee_id=rec.employee_id.id,
                project_id=rec.project_id.id, summary=_('SOP marked as read'), record=rec)
        return True

    def action_mark_training(self):
        self.write({'training_done': True})
        for rec in self:
            self.env['epo.timeline.event'].log(
                event_type='training_done', employee_id=rec.employee_id.id,
                project_id=rec.project_id.id, summary=_('Training attended'), record=rec)
        return True

    def action_waive(self):
        """Skip the gate for one person on one project. GPM or Admin only, reason
        mandatory, and it is written to the audit log as well as the timeline — a waiver
        is the one place where somebody starts work without evidence they are ready."""
        # env.su covers migrations and internal automation running as superuser;
        # every human path still needs the GPM group.
        if not (self.env.su
                or self.env.user.has_group('ethara_project_os.group_epo_manager')):
            raise UserError(_('Only a GPM or an Admin may waive onboarding.'))
        reason = self.env.context.get('epo_reason')
        if not reason:
            raise UserError(_('A waiver needs a reason.'))
        now = fields.Datetime.now()
        for rec in self:
            rec.sudo().write({
                'waived_by_id': self.env.user.id,
                'waived_at': now,
                'waiver_reason': reason,
                'unlocked_at': rec.unlocked_at or now,
            })
            self.env['epo.audit.log'].record('onboarding_waived', rec, reason)
            self.env['epo.timeline.event'].log(
                event_type='onboarding_waived', employee_id=rec.employee_id.id,
                project_id=rec.project_id.id,
                summary=_('Onboarding waived: %s', reason), record=rec)
            rec._on_unlock()
        return True

    def to_dict(self):
        """The pod member's onboarding screen, in one payload."""
        self.ensure_one()
        project = self.project_id
        # The SOP step shows the SOP folder, not the whole Knowledge side. Before the
        # folders existed this listed every knowledge document, so a pod member clicking
        # "SOP" also got task videos and common errors with no way to tell them apart.
        sop_folder = self.env['epo.folder']._get(project, 'sop')
        docs = sop_folder.document_ids.filtered('active')
        other_knowledge = project.knowledge_doc_ids.filtered(
            lambda d: d.active and d.folder_id != sop_folder)
        trainings = project.training_ids.filtered('active')
        assessment = project.ethara_assessment_id
        return {
            'project_id': project.id,
            'project_name': project.name,
            'unlocked': self.unlocked,
            'blockers': self.blockers,
            'waived': bool(self.waived_by_id),
            'sop': {
                'done': self.sop_done,
                'done_at': str(self.sop_done_at or ''),
                'documents': [d.to_dict() for d in docs],
            },
            # The rest of the Knowledge side, so the screen can offer Common Errors and
            # Task Videos alongside the SOP without a second round trip.
            'knowledge': [d.to_dict() for d in other_knowledge],
            'training': {
                'required': self._training_required(),
                'done': self.training_done,
                'done_at': str(self.training_done_at or ''),
                'sessions': [{
                    'id': t.id, 'name': t.name, 'mode': t.mode, 'url': t.url or '',
                    'notes': t.notes or '', 'mandatory': t.is_mandatory,
                    'scheduled_at': str(t.scheduled_at or ''),
                } for t in trainings],
            },
            'assessment': {
                'required': self._assessment_required(),
                'passed': self.assessment_passed,
                'verified_by': self.assessment_verified_by.name or '',
                'verified_at': str(self.assessment_verified_at or ''),
                'score': self.assessment_score or None,
                # Where the tasker goes to sit it. Odoo never sees the result — a PM
                # reads it there and ticks the box here.
                'link': assessment.to_dict() if assessment else None,
            },
        }
