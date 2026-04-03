from odoo import models, fields, api


class TaskForgeAllocation(models.Model):
    _name = 'task.forge.allocation'
    _description = 'Task Forge Project Allocation'
    _order = 'create_date desc'

    project_id = fields.Many2one(
        'project.project',
        string='Project',
        required=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Tasker',
        required=True,
        ondelete='cascade',
    )
    allocated_by_id = fields.Many2one(
        'hr.employee',
        string='Allocated By',
    )
    is_tested = fields.Boolean(string='Is Tested', default=False)
    is_visible_on_multimango = fields.Boolean(string='Visible on Multimango', default=False)

    project_name = fields.Char(related='project_id.name', store=True)
    employee_name = fields.Char(related='employee_id.name', store=True)
    project_status = fields.Selection(related='project_id.task_forge_status', store=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.employee_id.sudo().write({'tf_allocation_status': 'allocated'})
        return records

    def unlink(self):
        employees = self.mapped('employee_id')
        res = super().unlink()
        for emp in employees:
            if not self.search_count([('employee_id', '=', emp.id)]):
                emp.sudo().write({'tf_allocation_status': 'unallocated'})
        return res
