from odoo import api, fields, models

# ir.config_parameter key holding the comma-separated res.users ids that are
# allowed to approve / reject job positions (and receive the approval emails).
CONFIG_PARAM_JOB_APPROVERS = 'ethara_hrms_extension.job_approver_user_ids'


def parse_user_ids(raw):
    """Parse the stored comma-separated id string into a clean list of ints."""
    if not raw:
        return []
    ids = []
    for token in str(raw).split(','):
        token = token.strip()
        if token.isdigit():
            ids.append(int(token))
    return ids


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    job_approver_user_ids = fields.Many2many(
        'res.users',
        'ethara_hrms_job_approver_settings_rel',
        'config_id',
        'user_id',
        string='Job Position Approvers',
        help="Users allowed to approve or reject job positions. When a JD is "
             "submitted for approval, the approval email (with View / Approve / "
             "Reject actions) is sent to every user listed here.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        ids = parse_user_ids(
            self.env['ir.config_parameter'].sudo().get_param(
                CONFIG_PARAM_JOB_APPROVERS, '',
            )
        )
        existing = self.env['res.users'].sudo().browse(ids).exists().ids if ids else []
        res['job_approver_user_ids'] = [(6, 0, existing)]
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            CONFIG_PARAM_JOB_APPROVERS,
            ','.join(str(i) for i in self.job_approver_user_ids.ids),
        )
