from odoo import api, fields, models


CONFIG_PARAM_APPROVERS = "etp_projects.token_purchase_approver_ids"
CONFIG_PARAM_FINANCE_USERS = "etp_projects.token_purchase_finance_user_ids"


def _parse_ids(param):
    return [int(x) for x in (param or "").split(",") if x.strip().isdigit()]


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    token_purchase_approver_ids = fields.Many2many(
        "res.users",
        "etp_token_purchase_approver_config_rel",
        "config_id",
        "user_id",
        string="Token Purchase Approvers",
        help="Users who receive token purchase approval emails and can approve/reject "
             "requests from the backend. Approval from any one of these users is sufficient.",
    )
    token_purchase_finance_user_ids = fields.Many2many(
        "res.users",
        "etp_token_purchase_finance_user_config_rel",
        "config_id",
        "user_id",
        string="Finance / Infra Team Users",
        help="Users notified after a token purchase request is approved. They receive "
             "a link to a public form for entering the approved amount, cost center, and "
             "supporting document.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        approver_ids = _parse_ids(ICP.get_param(CONFIG_PARAM_APPROVERS, default=""))
        finance_ids = _parse_ids(ICP.get_param(CONFIG_PARAM_FINANCE_USERS, default=""))
        res["token_purchase_approver_ids"] = [(6, 0, approver_ids)]
        res["token_purchase_finance_user_ids"] = [(6, 0, finance_ids)]
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(
            CONFIG_PARAM_APPROVERS,
            ",".join(str(i) for i in self.token_purchase_approver_ids.ids),
        )
        ICP.set_param(
            CONFIG_PARAM_FINANCE_USERS,
            ",".join(str(i) for i in self.token_purchase_finance_user_ids.ids),
        )
