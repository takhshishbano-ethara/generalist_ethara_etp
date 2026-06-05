from odoo import fields, models


class CastielDashboardConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    castiel_repo_url = fields.Char(
        string='Repository URL',
        config_parameter='castiel_dashboard.repo_url',
        default='https://www.ethara.ai',
    )
    castiel_paper_url = fields.Char(
        string='Paper URL',
        config_parameter='castiel_dashboard.paper_url',
        default='https://arxiv.org/abs/2506.02548',
    )
