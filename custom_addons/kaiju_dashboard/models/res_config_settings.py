from odoo import fields, models


class KaijuDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kaiju_trajectories_url = fields.Char(
        string="Trajectories URL",
        config_parameter="kaiju_dashboard.trajectories_url",
        help="URL for the Trajectories link button. "
        "Leave empty to use the default GitHub URL.",
    )
    kaiju_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="kaiju_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default HuggingFace URL.",
    )
