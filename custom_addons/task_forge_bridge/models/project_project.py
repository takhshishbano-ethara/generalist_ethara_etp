from odoo import models, fields, api


class ProjectProject(models.Model):
    _inherit = 'project.project'

    task_forge_status = fields.Selection(
        [('live', 'Live'), ('testing', 'Testing'), ('paused', 'Paused')],
        string='Task Forge Status',
        default='live',
    )
    task_forge_platform = fields.Char(
        string='Platform',
        default='Multimango',
    )
    task_forge_allocation_ids = fields.One2many(
        'task.forge.allocation',
        'project_id',
        string='Task Forge Allocations',
    )

    @api.model
    def _task_forge_live_domain(self):
        """Domain matching projects considered 'currently running'.

        Two independent status conventions coexist across Task Forge:
        ``task_forge_status == 'live'`` (task_forge_bridge) and
        ``non_stemp_project_status`` in the running set (project_extension,
        non-STEM). A project is treated as live when EITHER holds. Returns a
        fresh prefix-notation list so callers may append AND terms.
        """
        return [
            ('task_forge_status', '=', 'live'),
            ('non_stemp_project_status', 'in', ['production']),
        ]
