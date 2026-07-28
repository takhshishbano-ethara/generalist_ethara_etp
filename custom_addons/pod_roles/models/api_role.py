import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# Role xml_id -> canonical (name, user_type, sequence). Enforced on every sync
# so renames/order apply even though the XML records are noupdate="1".
# Sequence drives the display order: Admin, PM, PL, Tasker.
ROLE_DEFS = {
    'pod_roles.role_admin': ('Admin', 'admin', 1),
    'pod_roles.role_pm': ('PM', 'PM', 2),
    'pod_roles.role_pl': ('PL', 'PL', 3),
    'pod_roles.role_tasker': ('Tasker', 'Tasker', 4),
}

# Role xml_id -> list of api.role.line menu_names that role may SEE.
# ``None`` means "every menu line" (full access).
ROLE_MENUS = {
    'pod_roles.role_admin': None,
    'pod_roles.role_pl': [
        'dashboard', 'projects', 'mytask', 'alltasks', 'taskreview', 'bench',
        'tags', 'blocker', 'myblocker', 'analytics', 'team_overview',
        'attendance', 'nomination', 'child3', 'child2', 'etp_assessment',
    ],
    'pod_roles.role_pm': [
        'dashboard', 'projects', 'mytask', 'myblocker', 'attendance',
        'leave management', 'emp_reimbursement',
    ],
    'pod_roles.role_tasker': [
        'dashboard', 'projects', 'mytask', 'myblocker', 'attendance',
    ],
}

# res.users.login -> role xml_id. Applied only when the user already exists,
# so the sync stays safe on databases that don't have the demo/test users.
USER_ROLE_ASSIGNMENTS = {
    'admin@testing.com': 'pod_roles.role_admin',
    'pm@testing.com': 'pod_roles.role_pm',
    'pl@testing.com': 'pod_roles.role_pl',
    'tasker@testing.com': 'pod_roles.role_tasker',
}


class ApiRole(models.Model):
    _inherit = 'api.role'
    _order = 'sequence, id'

    # Pre-existing api_auth_gateway roles keep the default 10 and sort after the
    # pod roles (sequence 1-4), which float to the top in the requested order.
    sequence = fields.Integer(default=10)

    @api.model
    def _sync_pod_role_config(self):
        """Apply names, menu links, endpoint grants and user assignments for the
        pod roles. Idempotent: safe to run on every install and upgrade.

        Called from data/pod_roles_sync.xml via an <function> tag so the state
        reproduces on a fresh ``-i pod_roles`` as well as on ``-u pod_roles``
        (an <function> runs in both modes, unlike a post_init_hook).
        """
        env = self.env
        Line = env['api.role.line']
        Endpoint = env['api.endpoint']
        RoleEndpoint = env['api.role.endpoint']

        all_lines = Line.search([])
        all_endpoints = Endpoint.search([])

        # -- drop obsolete pod roles (renamed/removed in a later version) --
        # Odoo does not reliably orphan-delete noupdate data records, so remove
        # any api.role owned by this module that is no longer in ROLE_DEFS.
        valid_names = {xmlid.split('.', 1)[1] for xmlid in ROLE_DEFS}
        obsolete = env['ir.model.data'].search([
            ('module', '=', 'pod_roles'),
            ('model', '=', 'api.role'),
            ('name', 'not in', list(valid_names)),
        ])
        for imd in obsolete:
            role = env['api.role'].browse(imd.res_id).exists()
            if role:
                _logger.info('pod_roles: removing obsolete role %s', role.name)
                role.unlink()
            imd.unlink()

        for role_xmlid, (name, user_type, sequence) in ROLE_DEFS.items():
            role = env.ref(role_xmlid, raise_if_not_found=False)
            if not role:
                _logger.warning('pod_roles: role %s not found, skipped', role_xmlid)
                continue

            # -- enforce canonical name / user_type / order (beats noupdate) --
            if (role.name != name or role.user_type != user_type
                    or role.sequence != sequence):
                role.write({
                    'name': name,
                    'user_type': user_type,
                    'sequence': sequence,
                })

            # -- menu permissions (drives the app sidebar) --
            menu_names = ROLE_MENUS.get(role_xmlid, [])
            if menu_names is None:
                lines = all_lines
            else:
                wanted = set(menu_names)
                lines = all_lines.filtered(lambda l: l.menu_name in wanted)
            role.line_ids = [(6, 0, lines.ids)]

            # -- endpoint grants (API authorization); create only the missing --
            granted = set(
                RoleEndpoint.search([('role_id', '=', role.id)])
                .mapped('api_end_point_id').ids
            )
            new_grants = [
                {'role_id': role.id, 'api_end_point_id': ep.id, 'method': 'GET'}
                for ep in all_endpoints if ep.id not in granted
            ]
            if new_grants:
                RoleEndpoint.create(new_grants)

            _logger.info(
                'pod_roles: synced %s -> %s menus, %s endpoints',
                role.name, len(lines), len(all_endpoints),
            )

        # -- user -> role assignments (only for users that exist) --
        Users = env['res.users']
        for login, role_xmlid in USER_ROLE_ASSIGNMENTS.items():
            user = Users.search([('login', '=', login)], limit=1)
            role = env.ref(role_xmlid, raise_if_not_found=False)
            if user and role:
                user.user_role = role.id
                _logger.info('pod_roles: assigned %s -> %s', login, role.name)

        return True
