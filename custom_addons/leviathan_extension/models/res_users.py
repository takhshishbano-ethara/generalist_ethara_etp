from odoo import api, models


LEVIATHAN_ADMIN_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

LEVIATHAN_TASKER_ROLE_XMLIDS = (
    "api_auth_gateway.role_tasker_technical",
    "api_auth_gateway.role_tasker_stem",
    "api_auth_gateway.role_tasker_non_stem",
)

ADMIN_GROUP_XMLID = "leviathan.group_leviathan_admin"
USER_GROUP_XMLID = "leviathan.group_leviathan_user"


def _resolve_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._sync_leviathan_groups()
        return users

    def write(self, vals):
        result = super().write(vals)
        if "user_role" in vals:
            self._sync_leviathan_groups()
        return result

    def _sync_leviathan_groups(self):
        env = self.env
        admin_group = env.ref(ADMIN_GROUP_XMLID, raise_if_not_found=False)
        user_group = env.ref(USER_GROUP_XMLID, raise_if_not_found=False)
        if not admin_group and not user_group:
            return

        admin_role_ids = _resolve_ids(env, LEVIATHAN_ADMIN_ROLE_XMLIDS)
        tasker_role_ids = _resolve_ids(env, LEVIATHAN_TASKER_ROLE_XMLIDS)

        for user in self:
            role = user.user_role
            if not role:
                continue
            target_group = False
            other_group = False
            if role.id in admin_role_ids and admin_group:
                target_group = admin_group
                other_group = user_group
            elif role.id in tasker_role_ids and user_group:
                target_group = user_group
                other_group = admin_group
            if not target_group:
                continue
            commands = [(4, target_group.id)]
            if other_group and other_group.id in user.groups_id.ids:
                commands.append((3, other_group.id))
            user.sudo().write({"groups_id": commands})
