# -*- coding: utf-8 -*-
import re

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

# Basic well-formedness check — a real match against res.users is still the
# authoritative test; this only lets us give a clearer message for typos.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Kensei2TrackerTeamMember(models.Model):
    """A curated Kensei2 Tracker team roster.

    A team member is a thin link to an existing ``res.users`` — this model
    stores NO duplicate user data. Name, email, employee id, role, team and
    department are all read back from the user / HR employee via related or
    sudo-computed fields, so there is exactly one source of truth. Adding a
    member never creates an Odoo user; if the email does not resolve to one,
    the record is rejected.
    """
    _name = 'kensei2.tracker.team.member'
    _description = 'Kensei2 Tracker Team Member'
    _inherit = ['mail.thread']
    _order = 'date_added desc, id desc'
    _rec_name = 'member_name'
    # let Many2one pickers (e.g. the Task Allocation "Tasker") match by email too
    _rec_names_search = ['member_name', 'email']

    ROLE_SELECTION = [
        ('admin', 'Admin'),
        ('pl', 'PL'),
        ('ql', 'QL'),
        ('tasker', 'Tasker'),
        ('other', 'Other'),
    ]
    STATUS_SELECTION = [
        ('active', 'Active'),
        ('on_hold', 'On Hold'),
        ('inactive', 'Inactive'),
    ]

    # ------------------------------------------------------------------ #
    #  The member link — add a member by picking an existing Odoo user, by
    #  Name or by Email. ``user_id`` is the single source of truth;
    #  ``email_user_id`` is a writable mirror so the same person can be chosen
    #  from the Email dropdown; ``email`` is kept in sync (and still resolves a
    #  typed/imported address) so bulk import and seed data keep working.
    # ------------------------------------------------------------------ #
    user_id = fields.Many2one(
        'res.users', string='Name', store=True, index=True,
        ondelete='cascade', tracking=True,
        help='Existing Odoo user — pick by name here (or by email opposite). '
             'No new user is created.')
    email_user_id = fields.Many2one(
        'res.users', string='Email', related='user_id', readonly=False,
        store=False, help='Same member, chosen by email address.')
    email = fields.Char(
        string='Email', compute='_compute_email', inverse='_inverse_email',
        store=True, index=True, tracking=True,
        help='Login / email of the linked user (kept in sync automatically).')

    # ------------------------------------------------------------------ #
    #  Reused user / employee data — related or sudo-computed, never stored
    #  as an independent copy the user could edit out of sync.
    # ------------------------------------------------------------------ #
    member_name = fields.Char(
        string='Name', related='user_id.name', store=True, readonly=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', compute='_compute_employee_data',
        store=True, readonly=True)
    employee_ref = fields.Char(
        string='Employee ID', compute='_compute_employee_data',
        store=True, readonly=True)
    department_id = fields.Many2one(
        'hr.department', string='Department', compute='_compute_employee_data',
        store=True, readonly=True)
    team_id = fields.Many2one(
        'hr.employee', string='Team', compute='_compute_employee_data',
        store=True, readonly=True,
        help='Reporting manager — used as the team grouping.')
    role = fields.Selection(
        selection=ROLE_SELECTION, string='Role', compute='_compute_role',
        store=True, readonly=True, index=True, tracking=True)

    # ------------------------------------------------------------------ #
    #  Team-management specific (not user data)
    # ------------------------------------------------------------------ #
    assigned_pl_id = fields.Many2one(
        'res.users', string='Assigned PL', index=True, tracking=True,
        help='Project Lead this member reports to for Kensei2 Tracker work.')
    assigned_ql_id = fields.Many2one(
        'res.users', string='Assigned QL', index=True, tracking=True,
        help="Quality Lead responsible for reviewing this member's work.")
    status = fields.Selection(
        selection=STATUS_SELECTION, string='Status', default='active',
        required=True, index=True, tracking=True)
    date_added = fields.Datetime(
        string='Date Added', default=fields.Datetime.now, readonly=True, index=True)
    active = fields.Boolean(default=True)
    notes = fields.Text(string='Notes')

    # Odoo 19 dropped the old ``_sql_constraints`` list; SQL constraints are now
    # declared as ``models.Constraint`` table objects. One user = one member.
    _user_uniq = models.Constraint(
        'unique(user_id)',
        'This user is already a Kensei2 team member — duplicates are not allowed.',
    )

    # ------------------------------------------------------------------ #
    #  Computes
    # ------------------------------------------------------------------ #
    @api.depends('user_id', 'user_id.email', 'user_id.login')
    def _compute_email(self):
        """Mirror the linked user's email/login onto the stored ``email`` so the
        column stays populated for the list, search and bulk-import matching."""
        for rec in self:
            u = rec.user_id
            rec.email = (u.email or u.login) if u else False

    def _inverse_email(self):
        """Resolve a typed / imported email to an existing user (by login or
        email), batched. Import and seed create with only an ``email``, so this
        is what turns that address into the ``user_id`` link. Blank input is
        left untouched (never clears an already-picked user).
        """
        Users = self.env['res.users'].sudo()
        raw = {(rec.email or '').strip() for rec in self if rec.email}
        terms = list(raw | {e.lower() for e in raw})
        found = {}
        if terms:
            for u in Users.search(['|', ('login', 'in', terms), ('email', 'in', terms)]):
                if u.login:
                    found.setdefault(u.login.strip().lower(), u)
                if u.email:
                    found.setdefault(u.email.strip().lower(), u)
        for rec in self:
            if not rec.email:
                continue
            email = rec.email.strip()
            if not _EMAIL_RE.match(email):
                raise ValidationError(_(
                    "“%s” is not a valid email address. Enter the email or "
                    "login of an existing Odoo user.", rec.email))
            user = found.get(email.lower())
            if not user:
                raise ValidationError(_(
                    "No Odoo user is registered with the email “%s”, so this "
                    "person cannot be added to the Kensei2 team. A team member "
                    "must already exist as an Odoo user — no new user is "
                    "created.", email))
            rec.user_id = user

    @api.depends('user_id',
                 'user_id.employee_ids',
                 'user_id.employee_ids.department_id',
                 'user_id.employee_ids.parent_id',
                 'user_id.employee_ids.identification_id',
                 'user_id.employee_ids.barcode')
    def _compute_employee_data(self):
        """Pull HR details from the linked user's employee (sudo — Tracker
        managers need not have full HR access).

        Depends on the full HR path via ``employee_ids`` (whose inverse
        ``hr.employee.user_id`` is stored, so Odoo can reverse-traverse it) —
        this way the stored projections stay in sync when the employee changes
        department, manager or ID, not only when the user link itself changes.
        ``employee_ids`` is used rather than the non-stored, non-invertible
        ``employee_id`` computed field.
        """
        for rec in self:
            emp = False
            if rec.user_id:
                user = rec.user_id.sudo()
                emp = user.employee_id or user.employee_ids[:1]
            rec.employee_id = emp.id if emp else False
            rec.employee_ref = (emp.identification_id or emp.barcode) if emp else False
            rec.department_id = emp.department_id.id if emp and emp.department_id else False
            rec.team_id = emp.parent_id.id if emp and emp.parent_id else False

    @api.depends('user_id', 'user_id.group_ids')
    def _compute_role(self):
        """Derive Tasker/QL/PL/Admin from group membership (implied groups
        included via ``all_group_ids``), most-privileged first."""
        def gid(xmlid):
            grp = self.env.ref(xmlid, raise_if_not_found=False)
            return grp.id if grp else None

        ladder = [
            ('admin', {gid('base.group_system'), gid('kensei2.group_kensei2_admin'),
                       gid('etp_user_roles.group_it_admin'),
                       gid('etp_user_roles.group_hr_admin'), gid('etp_user_roles.group_cto')}),
            ('pl', {gid('etp_user_roles.group_project_lead'), gid('kensei2.group_kensei2_pl')}),
            ('ql', {gid('etp_user_roles.group_quality_lead'), gid('kensei2.group_kensei2_ql')}),
            ('tasker', {gid('etp_user_roles.group_tasker'), gid('kensei2.group_kensei2_tasker')}),
        ]
        ladder = [(role, {g for g in gids if g}) for role, gids in ladder]
        for rec in self:
            if not rec.user_id:
                rec.role = False
                continue
            user_groups = set(rec.user_id.sudo().all_group_ids.ids)
            rec.role = next((role for role, gids in ladder if user_groups & gids), 'other')

    # ------------------------------------------------------------------ #
    #  Validation / UX
    # ------------------------------------------------------------------ #
    def action_view_performance(self):
        """Open the per-tasker performance dashboard for this member (QL/PL)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'kensei2_tasker_dashboard',
            'name': _('%s — Performance', self.member_name or self.email or _('Tasker')),
            'params': {'member_id': self.id},
            'target': 'current',
        }

    @api.constrains('user_id')
    def _check_user_exists(self):
        """A team member must map to an existing Odoo user. Because the form
        only offers existing users in the Name/Email dropdowns, this mainly
        guards programmatic writes (the email inverse raises its own detailed
        message when a typed/imported address matches no user)."""
        for rec in self:
            if not rec.user_id:
                raise ValidationError(_(
                    "Select an existing Odoo user by Name or Email. "
                    "No new user is created."))

    # ------------------------------------------------------------------ #
    #  Create — push a success toast to the current user (a plain form save
    #  shows no confirmation on its own). Suppressed for bulk import, which
    #  has its own result summary (pass context ``kensei2_skip_toast``).
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('kensei2_skip_toast'):
            records._notify_added()
        return records

    def _notify_added(self):
        partner = self.env.user.partner_id
        if not partner:
            return
        role_labels = dict(self.ROLE_SELECTION)
        for rec in self:
            if not rec.user_id:
                continue
            self.env['bus.bus']._sendone(
                partner,
                'kensei2/team_member_added',
                {
                    'name': rec.member_name or rec.email or _('Member'),
                    'role': role_labels.get(rec.role, ''),
                },
            )

    # Fields whose change is a real, user-facing edit worth confirming. Excludes
    # the computed projections (member_name, role, email, employee_*) so a
    # recompute never fires a spurious "updated" toast.
    _TOAST_ON_WRITE = {
        'user_id', 'assigned_pl_id', 'assigned_ql_id', 'status', 'notes',
    }

    def write(self, vals):
        res = super().write(vals)
        if (not self.env.context.get('kensei2_skip_toast')
                and self._TOAST_ON_WRITE.intersection(vals)):
            self._notify_updated()
        return res

    def _notify_updated(self):
        partner = self.env.user.partner_id
        if not partner:
            return
        named = self if len(self) == 1 else self.browse()
        self.env['bus.bus']._sendone(
            partner,
            'kensei2/team_member_updated',
            {
                'name': named.member_name or named.email or '' if named else '',
                'count': len(self),
            },
        )
