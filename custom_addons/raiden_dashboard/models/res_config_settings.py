from odoo import fields, models


class RaidenDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    raiden_trajectories_url = fields.Char(
        string="Harbor Framework URL",
        config_parameter="raiden_dashboard.trajectories_url",
        help="URL for the first dashboard link button. "
        "Leave empty to use the default placeholder.",
    )
    raiden_dataset_url = fields.Char(
        string="Agent SDK URL",
        config_parameter="raiden_dashboard.dataset_url",
        help="URL for the second dashboard link button. "
        "Leave empty to use the default placeholder.",
    )
