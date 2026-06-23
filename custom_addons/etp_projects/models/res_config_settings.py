from odoo import api, fields, models

CONFIG_PARAM_DEFAULT_APPROVERS = "etp_projects.default_approver_user_ids"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    etp_default_approver_user_ids = fields.Many2many(
        "res.users",
        "etp_projects_default_approver_settings_rel",
        "config_id",
        "user_id",
        string="Default Project Budget Approvers",
        help="Users automatically added as approvers when a new Project Budget is created "
             "(both via the Odoo form view and the /api/v1/etp_projects/project_budget/create API). "
             "API callers can supply additional approver_ids — they are merged with this list.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        ids = _parse_default_approver_ids(
            self.env["ir.config_parameter"].sudo().get_param(
                CONFIG_PARAM_DEFAULT_APPROVERS, "",
            )
        )
        existing = self.env["res.users"].sudo().browse(ids).exists().ids if ids else []
        res["etp_default_approver_user_ids"] = [(6, 0, existing)]
        return res

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            CONFIG_PARAM_DEFAULT_APPROVERS,
            ",".join(str(i) for i in self.etp_default_approver_user_ids.ids),
        )


def _parse_default_approver_ids(raw):
    if not raw:
        return []
    ids = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            ids.append(int(token))
    return ids
