from odoo import models, fields, api


class ProjectResponseConfig(models.Model):
    _name = 'project.response.config'
    _description = 'Project Response Field Configuration'
    _order = 'sequence, id'

    project_id = fields.Many2one(
        'project.project', string='Project',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(string='Sequence', required=True, default=1)
    label = fields.Char(string='Label', required=True)

    _sql_constraints = [
        ('uniq_project_sequence', 'unique(project_id, sequence)',
         'Each response field sequence must be unique per project.'),
    ]

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

    @api.model
    def generate_configs_for_project(self, project_id, count):
        """Create N response config records for a project, auto-labeling A, B, C..."""
        vals_list = []
        for i in range(1, count + 1):
            vals_list.append({
                'project_id': project_id,
                'sequence': i,
                'label': self._generate_label(i),
            })
        if vals_list:
            return self.create(vals_list)
        return self.browse()

    @api.model
    def sync_configs_for_project(self, project_id, new_count):
        """Adjust response configs to match new_count (add/remove as needed)."""
        existing = self.search(
            [('project_id', '=', project_id)], order='sequence'
        )
        current_count = len(existing)

        if new_count > current_count:
            vals_list = []
            for i in range(current_count + 1, new_count + 1):
                vals_list.append({
                    'project_id': project_id,
                    'sequence': i,
                    'label': self._generate_label(i),
                })
            self.create(vals_list)
        elif new_count < current_count:
            to_remove = existing[new_count:]
            to_remove.unlink()
