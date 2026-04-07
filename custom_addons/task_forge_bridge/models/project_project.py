from odoo import models, fields


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
