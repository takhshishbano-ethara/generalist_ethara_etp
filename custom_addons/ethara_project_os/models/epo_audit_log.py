"""Targeted audit, not blanket audit.

Odoo's own ``mail.tracking.value`` already records ORM-level field changes on tracked
models. This table is for the handful of actions where *the business* needs an answer
independent of the chatter: an admin voiding a submission, unlocking a payroll-closed
roster day, waiving onboarding, or resetting an assessment.

Admin-only, by record rule. Append-only, by method override.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EpoAuditLog(models.Model):
    _name = 'epo.audit.log'
    _description = 'Project OS Audit Log'
    _order = 'occurred_at desc, id desc'
    _rec_name = 'action'

    occurred_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    actor_id = fields.Many2one(
        'res.users', required=True, ondelete='restrict', default=lambda s: s.env.user)
    action = fields.Char(required=True)
    res_model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    reason = fields.Text(required=True, help='Never optional. An unexplained override '
                                             'is indistinguishable from a mistake.')
    old_values_json = fields.Text()
    new_values_json = fields.Text()

    @api.model
    def record(self, action, target, reason, old_values=None, new_values=None):
        return self.sudo().create({
            'action': action,
            'res_model': target._name,
            'res_id': target.id,
            'reason': reason,
            'old_values_json': json.dumps(old_values, default=str) if old_values else False,
            'new_values_json': json.dumps(new_values, default=str) if new_values else False,
        })

    def write(self, vals):
        raise UserError(_('The audit log is append-only.'))

    def unlink(self):
        raise UserError(_('The audit log is append-only.'))
