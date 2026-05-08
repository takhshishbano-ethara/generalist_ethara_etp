from odoo import fields, models


class SurtorDashboardConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    surtor_trajectories_url = fields.Char(
        string='Trajectories URL',
        config_parameter='surtor_dashboard.trajectories_url',
        default='https://github.com/Ethara-Ai/surtor',
    )
    surtor_dataset_url = fields.Char(
        string='Dataset URL',
        config_parameter='surtor_dashboard.dataset_url',
        default='https://huggingface.co/datasets/ethara/Surtor',
    )
