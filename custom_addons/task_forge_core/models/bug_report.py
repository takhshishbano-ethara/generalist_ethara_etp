from odoo import models, fields


class TaskForgeBugReport(models.Model):
    _name = 'task.forge.bug.report'
    _description = 'Task Forge Bug Report'
    _order = 'create_date desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    task_id = fields.Many2one('task.forge.log', string='Task', required=True, ondelete='cascade')
    task_name = fields.Char(related='task_id.name', store=True)

    employee_id = fields.Many2one(
        'hr.employee', string='Reported By', required=True,
        default=lambda self: self.env.user.employee_id,
    )
    qr_id = fields.Many2one('hr.employee', string='QR')
    pl_id = fields.Many2one('hr.employee', string='PL')

    video_url = fields.Char(string='Video URL')
    description = fields.Text(string='Description')
    state = fields.Selection([
        ('open', 'Open'),
        ('reviewed', 'Reviewed'),
        ('closed', 'Closed'),
    ], string='Status', default='open')

    employee_name = fields.Char(related='employee_id.name', store=True)

    def create(self, vals_list):
        if isinstance(vals_list, dict):
            vals_list = [vals_list]
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('task.forge.bug.report') or 'New'
        return super().create(vals_list)
