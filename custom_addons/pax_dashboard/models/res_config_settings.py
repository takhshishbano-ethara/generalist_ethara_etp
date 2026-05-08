from odoo import fields, models


class PaxDashboardConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pax_trajectories_url = fields.Char(
        string='Trajectories URL',
        config_parameter='pax_dashboard.trajectories_url',
        default='https://github.com/Ethara-Ai/pax',
    )
    pax_dataset_url = fields.Char(
        string='Dataset URL',
        config_parameter='pax_dashboard.dataset_url',
        default='https://huggingface.co/datasets/ethara/Pax',
    )
