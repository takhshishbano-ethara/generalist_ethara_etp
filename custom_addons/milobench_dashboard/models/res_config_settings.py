from odoo import fields, models


DEFAULT_GITHUB_URL = "https://github.com/EtharaOrion/milo-bench-samples"
DEFAULT_HUGGINGFACE_URL = "https://huggingface.co/datasets/ethara/milo-bench-samples"
DEFAULT_PAPER_URL = "https://github.com/EtharaOrion/milo-bench-samples#readme"


class MilobenchConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    milobench_github_url = fields.Char(
        string="Milo-Bench GitHub URL",
        config_parameter="milobench_dashboard.github_url",
        default=DEFAULT_GITHUB_URL,
        help="Source repository for the Milo-Bench samples dataset.",
    )
    milobench_huggingface_url = fields.Char(
        string="Milo-Bench HuggingFace URL",
        config_parameter="milobench_dashboard.huggingface_url",
        default=DEFAULT_HUGGINGFACE_URL,
        help="Public HuggingFace dataset URL for Milo-Bench samples.",
    )
    milobench_paper_url = fields.Char(
        string="Milo-Bench Paper / Report URL",
        config_parameter="milobench_dashboard.paper_url",
        default=DEFAULT_PAPER_URL,
        help="Long-form write-up of the benchmark methodology.",
    )
