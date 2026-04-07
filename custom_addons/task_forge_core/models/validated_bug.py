from odoo import models, fields


class TaskForgeValidatedBug(models.Model):
    _name = 'task.forge.validated.bug'
    _description = 'Task Forge Validated Bug'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Bug Title', required=True, tracking=True)
    blocker_id = fields.Many2one('task.forge.blocker', string='Source Blocker', ondelete='set null')
    task_id = fields.Many2one('task.forge.log', string='Task', ondelete='set null')
    project_id = fields.Many2one('project.project', string='Project')

    employee_id = fields.Many2one('hr.employee', string='Reported By')
    qr_id = fields.Many2one('hr.employee', string='QR')
    pl_id = fields.Many2one('hr.employee', string='PL')
    validated_by_id = fields.Many2one('hr.employee', string='Validated By')

    bug_description = fields.Text(string='Bug Description')
    steps_to_reproduce = fields.Text(string='Steps to Reproduce')
    pages_affected = fields.Text(string='Pages Affected')
    impact = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ], string='Impact', default='medium', tracking=True)
    impact_details = fields.Text(string='Impact Details')
    blocker_reason = fields.Text(string='Original Blocker Reason')

    qr_video_url = fields.Char(string='QR Video URL')
    qr_image_url = fields.Char(string='QR Image URL')

    state = fields.Selection([
        ('open', 'Open'),
        ('in_review', 'In Review'),
        ('fixed', 'Fixed'),
        ('closed', 'Closed'),
    ], string='Status', default='open', tracking=True)

    employee_name = fields.Char(related='employee_id.name', store=True)
    project_name = fields.Char(related='project_id.name', store=True)
    task_name = fields.Char(related='task_id.name', store=True)
