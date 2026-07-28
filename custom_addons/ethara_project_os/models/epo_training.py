"""Training sessions — live or recorded.

A project may have several (kickoff walkthrough, tooling session, edge cases). Only the
mandatory ones gate onboarding; the rest are reference. A project with no mandatory
training auto-passes that step, exactly as the prototype did — the gate must not block
on a stage the PM never set up.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

URL_RE = re.compile(r'^https?://', re.IGNORECASE)


class EpoTraining(models.Model):
    _name = 'epo.training'
    _description = 'Project OS Training Session'
    _order = 'project_id, sequence, id'

    project_id = fields.Many2one(
        'project.project', required=True, ondelete='cascade', index=True,
        domain=[('is_project_os', '=', True)])
    name = fields.Char(required=True, default='Training session')
    mode = fields.Selection(
        [('online', 'Online (live)'), ('recorded', 'Recorded')],
        required=True, default='recorded')
    url = fields.Char(help='Meet link for a live session, recording link otherwise.')
    notes = fields.Text()
    scheduled_at = fields.Datetime()
    duration_mins = fields.Integer()
    trainer_id = fields.Many2one('hr.employee', string='Trainer', ondelete='restrict')
    is_mandatory = fields.Boolean(
        default=True, help='Mandatory sessions gate onboarding for this project.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _duration_sane = models.Constraint(
        'CHECK (duration_mins IS NULL OR (duration_mins > 0 AND duration_mins <= 1440))',
        'Training duration must be between 1 minute and 24 hours.')
    _online_needs_schedule = models.Constraint(
        "CHECK (mode <> 'online' OR scheduled_at IS NOT NULL)",
        'A live training session needs a scheduled date and time.')

    @api.constrains('url')
    def _check_url(self):
        for rec in self:
            if rec.url and not URL_RE.match(rec.url):
                raise ValidationError(_(
                    'Training links must start with http:// or https://.'))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            self.env['epo.timeline.event'].log(
                event_type='training_set', project_id=rec.project_id.id,
                summary=_('Training set: %s', rec.name), record=rec)
        return records
