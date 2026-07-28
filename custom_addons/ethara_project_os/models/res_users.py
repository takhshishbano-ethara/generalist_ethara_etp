"""The login side of a person.

Everything the API needs to answer "who am I and what may I see" hangs off here, so
neither the controllers nor the record rules ever have to trust a client-supplied
"who am I".
"""

from odoo import api, fields, models

from .epo_role_map import ROLE_GROUPS, level_for_api_role


class ResUsers(models.Model):
    _inherit = 'res.users'

    epo_role = fields.Selection(
        related='employee_id.epo_role', string='Project OS Role', readonly=True)
    epo_pod_id = fields.Many2one(related='employee_id.epo_pod_id', readonly=True)

    # ------------------------------------------------------------------
    # role derivation — api.role is the registry, this module only maps it
    # ------------------------------------------------------------------
    def _epo_level_from_api_role(self):
        """The Project OS level this user's ``user_role`` maps to, or ``False``.

        ``api.role`` is the deployment's role registry (api_auth_gateway); Project OS
        does not keep its own. Returns False while the four levels have not been created
        there yet, which is what makes the fallback below necessary.
        """
        self.ensure_one()
        if 'user_role' not in self._fields:
            return False
        return level_for_api_role(self.env, self.user_role)

    def _epo_sync_groups(self):
        """Put each user in the group their role implies, and only that one.

        Two sources, in priority order:

        1. ``user_role`` → the registry. Authoritative once it maps to a level.
        2. ``epo.role.assignment`` → the transitional fallback, so nobody loses access
           on the day this ships or while api.role is still being populated.

        Removing the groups a user should no longer hold matters as much as adding the
        right one: a lapsed Pod Lead who keeps the group keeps their pod's roster.
        """
        all_groups = self.env['res.groups']
        for xmlid in ROLE_GROUPS.values():
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                all_groups |= group

        Assignment = self.env['epo.role.assignment'].sudo()
        for user in self:
            wanted = self.env['res.groups']
            level = user._epo_level_from_api_role()
            if level:
                # The registry has an answer — every weaker level is implied by the
                # group ladder, so one group is enough.
                group = self.env.ref(ROLE_GROUPS[level], raise_if_not_found=False)
                if group:
                    wanted |= group
            else:
                employee = user._epo_employee()
                if employee:
                    for role in Assignment.search([
                            ('employee_id', '=', employee.id),
                            ('is_current', '=', True)]).mapped('role'):
                        group = self.env.ref(ROLE_GROUPS[role], raise_if_not_found=False)
                        if group:
                            wanted |= group
            commands = ([(3, g.id) for g in (all_groups - wanted)]
                        + [(4, g.id) for g in wanted])
            if commands:
                # A plain write, NOT super(ResUsers, ...): seven other modules extend
                # res.users, and skipping to the next class in the MRO would bypass
                # theirs (base_user_role.write, for one, re-derives groups from its own
                # roles). Recursion is not a risk — the override below only reacts to
                # `user_role`, and this writes `group_ids`.
                user.sudo().write({'group_ids': commands})

    def write(self, vals):
        """A change of ``user_role`` has to move the Project OS groups with it.

        The field belongs to api_auth_gateway and is written from several places — the
        HR import, the REST layer, a CSV load. None of them know about this module, so
        the sync has to hang off the write rather than off a button here.
        """
        result = super().write(vals)
        if 'user_role' in vals and not self.env.context.get('epo_skip_role_sync'):
            self.with_context(epo_skip_role_sync=True)._epo_sync_groups()
        return result

    def _epo_role(self):
        """The acting role, resolved from group membership rather than the employee
        record — a user with the Admin group but no employee row is still an admin.

        Group membership is itself derived from user_role (falling back to
        epo.role.assignment), so the two agree; this order just means the API keeps
        working for service accounts."""
        self.ensure_one()
        if self.has_group('ethara_project_os.group_epo_admin'):
            return 'admin'
        if self.has_group('ethara_project_os.group_epo_pm'):
            return 'pm'
        if self.has_group('ethara_project_os.group_epo_pod_lead'):
            return 'pl'
        if self.has_group('ethara_project_os.group_epo_tasker'):
            return 'tasker'
        return False

    def _epo_employee(self):
        self.ensure_one()
        emp = self.employee_id
        if not emp:
            emp = self.env['hr.employee'].sudo().search([('user_id', '=', self.id)], limit=1)
        return emp

    def _epo_scope_employee_ids(self):
        """Employee ids visible to this login. See hr.employee._epo_scope_employee_ids."""
        self.ensure_one()
        role = self._epo_role()
        if role in ('pm', 'admin'):
            return self.env['hr.employee'].sudo().search([]).ids
        emp = self._epo_employee()
        if not emp:
            return []
        if role == 'pl':
            pods = self.env['epo.pod'].sudo().search([('lead_employee_id', '=', emp.id)])
            peers = self.env['hr.employee'].sudo().search([
                '|', ('epo_pod_id', 'in', pods.ids), ('parent_id', '=', emp.id)])
            return (peers | emp).ids
        return emp.ids

    def _epo_accessible_project_ids(self):
        """Projects this login may read the knowledge folder of.

        PM/Admin see every project. A PL or Tasker sees only the projects someone in their
        scope is (or has been) allocated to — the knowledge folder is project IP, not
        a public library."""
        self.ensure_one()
        if self._epo_role() in ('pm', 'admin'):
            # Only the projects that actually run through this pipeline. The registry
            # also holds projects owned by the budget side; they have no knowledge
            # folder to read and do not belong in a Project OS listing.
            return self.env['project.project'].sudo().search(
                [('is_project_os', '=', True)]).ids
        emp_ids = self._epo_scope_employee_ids()
        allocs = self.env['epo.allocation'].sudo().search([('employee_id', 'in', emp_ids)])
        return allocs.mapped('project_id').ids
