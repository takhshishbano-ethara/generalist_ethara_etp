from odoo import models, fields, api


class TaskForgeResponse(models.Model):
    _name = 'task.forge.response'
    _description = 'Task Forge Response'
    _order = 'sequence, id'

    task_id = fields.Many2one(
        'task.forge.log', string='Task Log',
        required=True, ondelete='cascade', index=True,
    )
    label = fields.Char(string='Label', required=True)
    sequence = fields.Integer(string='Sequence', required=True, default=1)
    value = fields.Text(string='Response Value')
    response_url = fields.Char(string='Response URL')

    project_id = fields.Many2one(
        related='task_id.project_id', store=True, string='Project',
    )
    employee_id = fields.Many2one(
        related='task_id.employee_id', store=True, string='Employee',
    )

    _uniq_task_sequence = models.Constraint(
        'UNIQUE(task_id, sequence)',
        'Each response sequence must be unique per task.',
    )

    @api.model
    def scaffold_for_task(self, task):
        """Create empty response records for a task based on its project's response count."""
        project = task.project_id
        if not project or not project.is_response_required:
            return self.browse()

        no_of_responses = project.no_of_responses or 0
        if not no_of_responses:
            return self.browse()

        existing_sequences = set(task.response_ids.mapped('sequence'))
        vals_list = []
        for i in range(1, no_of_responses + 1):
            if i not in existing_sequences:
                vals_list.append({
                    'task_id': task.id,
                    'label': self._generate_label(i),
                    'sequence': i,
                    'value': False,
                })
        if vals_list:
            return self.create(vals_list)
        return self.browse()

    @staticmethod
    def _generate_label(sequence):
        """Convert 1-indexed sequence to letter label: 1->A, 2->B, ..., 27->AA"""
        label = ''
        n = sequence
        while n > 0:
            n -= 1
            label = chr(65 + n % 26) + label
            n //= 26
        return f"Response {label}"
