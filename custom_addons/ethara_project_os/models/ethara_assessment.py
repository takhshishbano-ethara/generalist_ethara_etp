"""The assessment — a link out, nothing more (data model v2 §4.6.2).

v2 drops the in-Odoo quiz engine entirely. The paper is authored and sat in an external
application — a Google Form, TestGorilla, an internal tool — and Odoo stores only the
link and the rules of engagement. There are no questions here, no options, no attempts
and no scoring, and §4.6.2's dropped-model list is deliberate:

* ``ethara.assessment.question`` — gone. The answer key never enters this database, so
  there is nothing to leak and nothing to hide behind a record rule.
* ``ethara.assessment.attempt`` — gone. Odoo does not grade, so it does not store
  attempts.

The verdict arrives by a human instead. A Tasker or Pod Lead reads the external result and
ticks ``assessment_passed`` on the onboarding record; that toggle is group-restricted and
audited through ``mail.thread`` (§4.6.3, §11.2). It is the one place in the module where
somebody's readiness is asserted rather than evidenced, which is exactly why it leaves a
trail.

``pass_score`` here is **informational** — it tells the tasker what they are aiming at
and the verifier what to look for. It is not enforced, because Odoo never sees the
score. The project's ``min_assessment_score`` is a different question and *is* enforced:
see ``project.project.candidates()``.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

URL_RE = re.compile(r'^https?://', re.IGNORECASE)


class EtharaAssessment(models.Model):
    _name = 'ethara.assessment'
    _description = 'Ethara Assessment (external link)'
    _order = 'project_id, sequence, id'
    _inherit = ['mail.thread']

    project_id = fields.Many2one(
        'project.project', required=True, ondelete='cascade', index=True,
        domain=[('is_project_os', '=', True)])
    title = fields.Char(
        required=True, default='External Assessment', tracking=True)
    url = fields.Char(
        required=True, tracking=True,
        help='Link to the external assessment application. This is the whole '
             'integration — nothing is imported back automatically.')
    provider = fields.Char(
        tracking=True,
        help='Who hosts it, e.g. Google Form, TestGorilla, internal.')
    pass_score = fields.Integer(
        default=60, tracking=True,
        help='The pass mark the external application applies, as a percentage. '
             'INFORMATIONAL ONLY — Odoo never sees the score, so it cannot enforce '
             'this. It tells the tasker what to aim at and the verifier what to look '
             'for. The bar that IS enforced is the project\'s minimum score, checked '
             'when somebody is allocated.')
    notes = fields.Text(help='Instructions for the tasker: how to reach it, what to bring.')

    is_mandatory = fields.Boolean(
        default=True, tracking=True,
        help='Mandatory assessments gate onboarding for this project. A project with '
             'no mandatory assessment auto-passes that step — the gate must never block '
             'on a stage nobody set up.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _pass_bounds = models.Constraint(
        'CHECK (pass_score >= 0 AND pass_score <= 100)',
        'The pass score must be a percentage between 0 and 100.')

    @api.constrains('url')
    def _check_url(self):
        """A ``javascript:`` URL rendered into a Tasker's browser is a privilege escalation,
        not a typo — and this link is handed to every tasker on the project."""
        for record in self:
            if not URL_RE.match(record.url or ''):
                raise ValidationError(_(
                    'The assessment link must start with http:// or https:// — '
                    '"%(url)s" does not.', url=record.url or ''))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            self.env['epo.timeline.event'].log(
                event_type='assessment_linked', project_id=record.project_id.id,
                summary=_('Assessment linked: %s', record.title), record=record,
                payload={'url': record.url, 'provider': record.provider or '',
                         'pass_score': record.pass_score})
        records.mapped('project_id')._recompute_gate()
        return records

    def to_dict(self):
        """What a tasker is shown: where to go and what to aim at."""
        self.ensure_one()
        return {
            'id': self.id,
            'title': self.title,
            'url': self.url,
            'provider': self.provider or '',
            'pass_score': self.pass_score,
            'notes': self.notes or '',
            'is_mandatory': self.is_mandatory,
        }
