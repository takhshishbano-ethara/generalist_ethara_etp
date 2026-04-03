from odoo import models, fields


class TaskForgeFlag(models.Model):
    _name = 'task.forge.flag'
    _description = 'Task Forge Flag'
    _order = 'create_date desc'

    name = fields.Char(string='Message', required=True)
    flag_type = fields.Selection([
        ('platform_issue', 'Platform Issue'),
        ('performance', 'Performance'),
        ('absence', 'Absence'),
    ], string='Type', required=True)
    severity = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string='Severity', default='medium')

    related_employee_id = fields.Many2one('hr.employee', string='Related Employee')
    related_project_id = fields.Many2one('project.project', string='Related Project')

    is_resolved = fields.Boolean(string='Resolved', default=False)
    resolved_at = fields.Datetime(string='Resolved At')

    related_employee_name = fields.Char(related='related_employee_id.name', store=True)
    related_project_name = fields.Char(related='related_project_id.name', store=True)

    def action_resolve(self):
        self.write({
            'is_resolved': True,
            'resolved_at': fields.Datetime.now(),
        })
