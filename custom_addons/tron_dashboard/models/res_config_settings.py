from odoo import fields, models


class TronDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    tron_trajectories_url = fields.Char(
        string="Trajectories URL",
        config_parameter="tron_dashboard.trajectories_url",
        help="URL for the Trajectories link button. "
        "Leave empty to use the default placeholder URL.",
    )
    tron_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="tron_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default placeholder URL.",
    )
