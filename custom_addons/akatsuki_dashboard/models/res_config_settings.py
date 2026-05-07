from odoo import fields, models


class AkatsukiDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    akatsuki_trajectories_url = fields.Char(
        string="Trajectories URL",
        config_parameter="akatsuki_dashboard.trajectories_url",
        help="URL for the Trajectories link button. "
        "Leave empty to use the default GitHub URL.",
    )
    akatsuki_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="akatsuki_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default HuggingFace URL.",
    )
