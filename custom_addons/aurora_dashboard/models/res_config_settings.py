from odoo import fields, models


class AuroraDashboardConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    aurora_trajectories_url = fields.Char(
        string="Trajectories URL",
        config_parameter="aurora_dashboard.trajectories_url",
        help="URL for the Trajectories link button. "
        "Leave empty to use the default placeholder.",
    )
    aurora_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="aurora_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default placeholder.",
    )
