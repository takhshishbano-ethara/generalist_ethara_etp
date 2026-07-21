from odoo import fields, models


class KangDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kang_trajectories_url = fields.Char(
        string="Repository URL",
        config_parameter="kang_dashboard.trajectories_url",
        help="URL for the Repository link button. "
        "Leave empty to use the default GitHub URL.",
    )
    kang_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="kang_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default HuggingFace URL.",
    )
