from odoo import fields, models


class DrengrDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    drengr_github_url = fields.Char(
        string="GitHub URL",
        config_parameter="drengr_dashboard.github_url",
        help="URL for the GitHub link button. "
        "Leave empty to use the default URL.",
    )
    drengr_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="drengr_dashboard.dataset_url",
        help="URL for the HuggingFace Dataset link button. "
        "Leave empty to use the default URL.",
    )
    drengr_paper_url = fields.Char(
        string="Paper URL",
        config_parameter="drengr_dashboard.paper_url",
        help="URL for the arXiv paper link button. "
        "Leave empty to use the default URL.",
    )
