from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    project_classification = fields.Selection(
        selection=[
            ('internal', 'Internal'),
            ('external', 'External'),
        ],
        string='Project Classification',
        default='internal',
        required=True,
        index=True,
        help="Internal projects are managed from the Project app and their "
             "tasks appear in the Tasks menus. External projects are managed "
             "from the dedicated External Projects app.",
    )
    api_map_ids = fields.One2many(
        comodel_name='etp.external.project.api.map',
        inverse_name='project_id',
        string='API Mappings',
        help="API table/field mappings connected to this external project.",
    )
    connected_table = fields.Char(string='Connected Table')
    category_url = fields.Char(string='Category URL')
    tasker_url = fields.Char(string='Tasker URL')
    budget_refresh_url = fields.Char(string='Budget Refresh URL')
