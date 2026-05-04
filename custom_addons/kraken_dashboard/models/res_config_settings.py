from odoo import fields, models


class KrakenDashboardConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    kraken_trajectories_url = fields.Char(
        string='Trajectories URL',
        config_parameter='kraken_dashboard.trajectories_url',
        default='https://github.com/Ethara-Ai/Kraken-Dataset',
    )
    kraken_dataset_url = fields.Char(
        string='Dataset URL',
        config_parameter='kraken_dashboard.dataset_url',
        default='https://huggingface.co/datasets/ethara/Kraken',
    )
