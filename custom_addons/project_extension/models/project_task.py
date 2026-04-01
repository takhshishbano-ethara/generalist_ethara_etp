from odoo import models, fields, api
from datetime import timedelta


class BaseProjectTask(models.Model):
    _name = 'base.project.task'
    _description = 'Base Project Task'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Blocker Name', tracking=True)
    sequence = fields.Char(string='Sequence', tracking=True)
    project_id = fields.Many2one('project.project', string='Project')
    status = fields.Selection([('in_progress', 'In Progress'), ('qc_review', 'QC Review'), ('reword', 'Rework'), ('queued', 'Queued'), ('assigned', 'Assigned'), ('rejected', 'Rejected'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('delivery', 'Delivery'), ('draft', 'Draft')], string='Status', default='draft')
    assignee_ids = fields.Many2many('hr.employee','base_project_task_hr_employee_rel', string='Assignees')
    priority = fields.Selection([('P1', 'Critical'), ('P2', 'High'), ('P3', 'Medium'), ('P4', 'Low'), ('P5', 'Minimal')], string='Priority')
    tags = fields.Many2many('project.task.tags', string='Tags')
    start_date = fields.Datetime(string='Start Date')
    end_date = fields.Datetime(string='End Date')
    is_sample_task = fields.Boolean(string='Is Sample Task', default=False)


    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['sequence'] = self.env['ir.sequence'].next_by_code('base.project.task')
        tasks = super(BaseProjectTask, self).create(vals_list)
        return tasks


class ProjectTaskTags(models.Model):
    _name = 'project.task.tags'
    _description = 'Project Task Tags'

    name = fields.Char(string='Name')