from odoo import fields, models


class HuskarlDashboardConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    huskarl_github_url = fields.Char(
        string='GitHub URL',
        config_parameter='huskarl_dashboard.github_url',
        default='https://github.com/harbor-framework/harbor',
    )
    huskarl_docs_url = fields.Char(
        string='Documentation URL',
        config_parameter='huskarl_dashboard.docs_url',
        default='https://harborframework.com/docs',
    )
