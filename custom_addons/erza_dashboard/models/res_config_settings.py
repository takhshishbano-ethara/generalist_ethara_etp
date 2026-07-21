from odoo import fields, models


DEFAULT_GITHUB_URL = "https://github.com/Ethara-Ai/erza-delivery/"
DEFAULT_HUGGINGFACE_URL = "https://huggingface.co/datasets/ethara/erza-samples"
DEFAULT_PAPER_URL = "https://github.com/Ethara-Ai/erza#readme"


class ErzaConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    erza_github_url = fields.Char(
        string="Erza GitHub URL",
        config_parameter="erza_dashboard.github_url",
        default=DEFAULT_GITHUB_URL,
        help="Client-facing delivery repository for the Erza benchmark.",
    )
    erza_huggingface_url = fields.Char(
        string="Erza HuggingFace URL",
        config_parameter="erza_dashboard.huggingface_url",
        default=DEFAULT_HUGGINGFACE_URL,
        help="Public HuggingFace dataset for the Erza sample bundle(s).",
    )
    erza_paper_url = fields.Char(
        string="Erza Paper / Report URL",
        config_parameter="erza_dashboard.paper_url",
        default=DEFAULT_PAPER_URL,
        help="Long-form write-up of the paired-evaluation methodology.",
    )
