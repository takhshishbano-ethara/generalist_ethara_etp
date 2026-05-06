from odoo import fields, models


class LokiDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    loki_dashboard_title = fields.Char(
        string="Dashboard Title",
        config_parameter="loki_dashboard.title",
        help="Override the dashboard page title. Leave empty to use the default.",
    )
