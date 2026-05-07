from odoo import fields, models


class VestaDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    vesta_github_url = fields.Char(
        string="GitHub URL",
        config_parameter="vesta_dashboard.github_url",
        help="URL for the GitHub link button. "
        "Leave empty to use the default URL.",
    )
    vesta_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="vesta_dashboard.dataset_url",
        help="URL for the HuggingFace Dataset link button. "
        "Leave empty to use the default URL.",
    )
