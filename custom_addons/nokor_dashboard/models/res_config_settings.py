from odoo import fields, models


class TerraDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    nokor_github_url = fields.Char(
        string="GitHub URL",
        config_parameter="nokor_dashboard.github_url",
        help="URL for the GitHub link button. "
        "Leave empty to use the default placeholder.",
    )
    nokor_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="nokor_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default placeholder.",
    )
