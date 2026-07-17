from odoo import fields, models


class RinzlerDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    rinzler_harness_url = fields.Char(
        string="yc-bench Harness URL",
        config_parameter="rinzler_dashboard.harness_url",
        help="URL for the first dashboard link button. "
        "Leave empty to use the default placeholder.",
    )
    rinzler_dataset_url = fields.Char(
        string="Rinzler Dataset URL",
        config_parameter="rinzler_dashboard.dataset_url",
        help="URL for the second dashboard link button. "
        "Leave empty to use the default placeholder.",
    )
