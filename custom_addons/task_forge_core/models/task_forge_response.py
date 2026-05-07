from odoo import models, fields, api


class TaskForgeResponse(models.Model):
    _name = 'task.forge.response'
    _description = 'Task Forge Response'
    _order = 'sequence, id'

    task_id = fields.Many2one(
        'task.forge.log', string='Task Log',
        required=True, ondelete='cascade', index=True,
    )
    config_id = fields.Many2one(
        'project.response.config', string='Response Config',
        required=True, ondelete='restrict',
    )
    label = fields.Char(string='Label (Snapshot)', required=True)
    sequence = fields.Integer(string='Sequence', required=True, default=1)
    value = fields.Text(string='Response Value')
    response_url = fields.Char(string='Response URL')

    project_id = fields.Many2one(
        related='task_id.project_id', store=True, string='Project',
    )
    employee_id = fields.Many2one(
        related='task_id.employee_id', store=True, string='Employee',
    )

    _uniq_task_config = models.Constraint(
        'UNIQUE(task_id, config_id)',
        'Each response config can only appear once per task.',
    )

    @api.model
    def scaffold_for_task(self, task):
        """Create empty response records for a task based on its project's config."""
        project = task.project_id
        if not project or not project.is_response_required:
            return self.browse()

        configs = self.env['project.response.config'].search(
            [('project_id', '=', project.id)], order='sequence'
        )
        if not configs:
            return self.browse()

        existing_config_ids = task.response_ids.mapped('config_id').ids
        vals_list = []
        for cfg in configs:
            if cfg.id not in existing_config_ids:
                vals_list.append({
                    'task_id': task.id,
                    'config_id': cfg.id,
                    'label': cfg.label,
                    'sequence': cfg.sequence,
                    'value': False,
                })
        if vals_list:
            return self.create(vals_list)
        return self.browse()
