from odoo import fields, models


class EtharaProjectsHubConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    projects_hub_kaiju_url = fields.Char(
        string="Kaiju URL",
        config_parameter="ethara_projects_hub.kaiju_url",
        help="URL for the Kaiju project dashboard. "
        "Leave empty to use the default.",
    )
    projects_hub_kraken_url = fields.Char(
        string="Kraken URL",
        config_parameter="ethara_projects_hub.kraken_url",
        help="URL for the Kraken project dashboard. "
        "Leave empty to use the default.",
    )
    projects_hub_aurora_url = fields.Char(
        string="Aurora URL",
        config_parameter="ethara_projects_hub.aurora_url",
        help="URL for the Aurora project dashboard. "
        "Leave empty to use the default.",
    )
    projects_hub_valkyrie_url = fields.Char(
        string="Valkyrie URL",
        config_parameter="ethara_projects_hub.valkyrie_url",
        help="URL for the Valkyrie project dashboard. "
        "Leave empty to use the default.",
    )
    projects_hub_tesseract_url = fields.Char(
        string="Tesseract URL",
        config_parameter="ethara_projects_hub.tesseract_url",
        help="URL for the Tesseract project dashboard. "
        "Leave empty to use the default.",
    )
