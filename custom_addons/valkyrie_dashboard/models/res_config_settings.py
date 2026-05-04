from odoo import fields, models


class ValkyrieDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    valkyrie_github_url = fields.Char(
        string="GitHub URL",
        config_parameter="valkyrie_dashboard.github_url",
        help="URL for the GitHub link button. "
        "Leave empty to use the default GitHub URL.",
    )
    valkyrie_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="valkyrie_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default HuggingFace URL.",
    )
