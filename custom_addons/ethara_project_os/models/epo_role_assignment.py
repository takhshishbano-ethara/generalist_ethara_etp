"""Effective-dated role grants — the transitional fallback, not the registry.

``api.role`` (api_auth_gateway) is this deployment's role registry, and Project OS
derives its groups from ``res.users.user_role`` — see ``epo_role_map``. This model
remains only for people whose ``user_role`` does not yet map to a Project OS level, so
that nobody loses access while that registry is being populated. Once the four
``api.role`` records exist and ``user_role`` is set, it can be retired.

What it still gives that ``user_role`` cannot: effective dating, and more than one role
at once.

The role is NOT a column on the employee. "Who was Pod Lead when this submission was
reviewed?" is a real question during an audit, and a mutable column cannot answer it.
A grant has a date range; the *current* grant drives the Odoo group membership, and the
closed grants stay as history.

Granting a role here is the single way a person gets an Ethara Project OS login level —
writing the record syncs ``res.users.group_ids`` so the two can never disagree.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# The map and the ladder live in epo_role_map, next to the api.role wiring, so there is
# one definition of "what a Project OS level is" rather than two that can drift.
from .epo_role_map import ROLE_GROUPS, ROLE_RANK  # noqa: F401  (re-exported)


class EpoRoleAssignment(models.Model):
    _name = 'epo.role.assignment'
    _description = 'Project OS Role Assignment'
    _inherit = ['mail.thread']
    _order = 'date_from desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True)
    user_id = fields.Many2one(related='employee_id.user_id', store=True, string='Login')
    role = fields.Selection(
        [('tasker', 'Tasker'),
         ('pl', 'PL — Pod Lead'),
         ('pm', 'PM — Programme Manager'),
         ('admin', 'Admin')],
        required=True, tracking=True)

    scope_pod_id = fields.Many2one(
        'epo.pod', string='Pod Scope', ondelete='restrict',
        help='Only meaningful for a Pod Lead: which pod they lead. PM and Admin are '
             'org-wide by definition; a Tasker is scoped to themselves.')

    date_from = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    date_to = fields.Date(tracking=True, help='Empty means the grant is current.')
    is_current = fields.Boolean(compute='_compute_is_current', store=True, index=True)

    granted_by_id = fields.Many2one('hr.employee', string='Granted By', ondelete='restrict')
    reason = fields.Text()

    _range_sane = models.Constraint(
        'CHECK (date_to IS NULL OR date_to >= date_from)',
        'A role grant cannot end before it starts.')
    _scope_sane = models.Constraint(
        "CHECK (scope_pod_id IS NULL OR role = 'pl')",
        'Only a Pod Lead grant may be scoped to a pod.')
    # One person may hold two DIFFERENT roles at once — a PL who also tasks is normal.
    # Holding the SAME role twice over overlapping dates makes "when was this granted"
    # ambiguous, and only the database can reject that atomically.
    _no_overlap = models.Constraint(
        "EXCLUDE USING gist ("
        "employee_id WITH =, role WITH =, "
        "daterange(date_from, date_to, '[]') WITH &&)",
        'That person already holds this role over an overlapping period.')

    @api.depends('date_from', 'date_to')
    def _compute_is_current(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_current = bool(
                rec.date_from and rec.date_from <= today
                and (not rec.date_to or rec.date_to >= today))

    @api.constrains('employee_id', 'role', 'date_from', 'date_to')
    def _check_no_overlap(self):
        """One person may hold two *different* roles at once (a PL who also tasks is
        normal). Holding the SAME role twice over overlapping dates is a data error —
        it makes "when was this granted" ambiguous. The database enforces this too
        (EXCLUDE constraint, see hooks.py); this check gives a readable message first."""
        for rec in self:
            clash = self.search([
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
                ('role', '=', rec.role),
                ('date_from', '<=', rec.date_to or '9999-12-31'),
                '|', ('date_to', '=', False), ('date_to', '>=', rec.date_from),
            ], limit=1)
            if clash:
                raise ValidationError(_(
                    '%(name)s already holds the %(role)s role over that period '
                    '(from %(from)s).',
                    name=rec.employee_id.display_name, role=rec.role,
                    **{'from': clash.date_from}))

    @api.constrains('role', 'scope_pod_id')
    def _check_pod_lead_has_scope(self):
        for rec in self:
            if rec.role == 'pl' and not rec.scope_pod_id and not rec.employee_id.epo_pod_id:
                raise ValidationError(_(
                    'A Pod Lead grant needs a pod: either set the pod scope on the grant '
                    'or put %(name)s in a pod first.', name=rec.employee_id.display_name))

    # ------------------------------------------------------------------
    # group sync — the grant is the source of truth for the login level
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        recs._sync_groups()
        recs._log_timeline('role_granted')
        return recs

    def write(self, vals):
        res = super().write(vals)
        if {'role', 'date_from', 'date_to', 'employee_id'} & set(vals):
            self._sync_groups()
            if 'date_to' in vals and vals.get('date_to'):
                self._log_timeline('role_revoked')
        return res

    def _sync_groups(self):
        """Push the affected users through the single sync on res.users.

        The grant is no longer the authority — ``api.role`` is (see epo_role_map). This
        model is the transitional fallback for anybody whose ``user_role`` does not yet
        map to a Project OS level, so it delegates rather than writing groups itself.
        Two code paths writing group_ids is how the two sources start disagreeing.
        """
        users = self.mapped('employee_id.user_id')
        if users:
            users._epo_sync_groups()

    def _log_timeline(self, event_type):
        Timeline = self.env['epo.timeline.event'].sudo()
        for rec in self:
            Timeline.log(
                event_type=event_type,
                employee_id=rec.employee_id.id,
                summary=_('Role %(role)s %(verb)s', role=rec.role,
                          verb='granted' if event_type == 'role_granted' else 'revoked'),
                payload={'role': rec.role, 'date_from': str(rec.date_from or ''),
                         'date_to': str(rec.date_to or ''),
                         'pod': rec.scope_pod_id.display_name or ''},
            )

    def action_revoke(self):
        """End the grant today rather than deleting it — history must survive."""
        today = fields.Date.context_today(self)
        for rec in self:
            rec.date_to = max(today, rec.date_from) if rec.date_from else today
        return True
