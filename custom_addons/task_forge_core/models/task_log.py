from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError
from datetime import datetime, date


class TaskForgeLog(models.Model):
    _name = 'task.forge.log'
    _description = 'Task Forge Task Log'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Task Name', required=True, tracking=True)
    sequence = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    employee_id = fields.Many2one(
        'hr.employee', string='Tasker', required=True,
        default=lambda self: self.env.user.employee_id,
        tracking=True,
    )
    project_id = fields.Many2one('project.project', string='Project', tracking=True)
    date = fields.Date(string='Date', default=fields.Date.context_today, required=True)
    state = fields.Selection([
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('blocker', 'Blocker'),
        ('returned', 'Returned'),
        ('ack', 'Acknowledged'),
        ('escalated', 'Escalated'),
        ('overdue', 'Overdue'),
    ], string='Status', default='in_progress', tracking=True)

    start_time = fields.Datetime(string='Start Time')
    end_time = fields.Datetime(string='End Time')
    time_taken_mins = fields.Integer(
        string='Time Taken (mins)',
        compute='_compute_time_taken',
        store=True,
    )
    pause_time = fields.Char(string="Pause Time")
    start_screenshot_url = fields.Char(string='Start Screenshot URL')
    end_screenshot_url = fields.Char(string='End Screenshot URL')

    blocker_reason = fields.Text(string='Blocker Reason')
    quality_score = fields.Integer(string='Quality Score')
    prompt_justification = fields.Text(string='Prompt Justification')
    feedback_note = fields.Text(string='Feedback Note')

    blocker_ids = fields.One2many('task.forge.blocker', 'task_id', string='Blockers')
    bug_report_ids = fields.One2many('task.forge.bug.report', 'task_id', string='Bug Reports')

    employee_name = fields.Char(related='employee_id.name', store=True)
    project_name = fields.Char(related='project_id.name', store=True)
    image_url_lines = fields.One2many('task.forge.image', 'task_id', string="Image Url Lines")

    # Rating Syste
    task_score = fields.Integer(string='Task Score')
    comment = fields.Char(string='Comment')

    @api.depends('start_time', 'end_time')
    def _compute_time_taken(self):
        for rec in self:
            if rec.start_time and rec.end_time:
                delta = rec.end_time - rec.start_time
                rec.time_taken_mins = int(delta.total_seconds() / 60)
            else:
                rec.time_taken_mins = 0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('sequence', 'New') == 'New':
                vals['sequence'] = self.env['ir.sequence'].next_by_code('task.forge.log') or 'New'
        return super().create(vals_list)

    def _check_punch_in(self, employee_id):
        """Verify the employee has an active attendance record for today."""
        today = date.today()
        attendance = self.env['hr.attendance'].sudo().search([
            ('employee_id', '=', employee_id),
            ('check_in', '>=', datetime.combine(today, datetime.min.time())),
            ('check_in', '<', datetime.combine(today, datetime.max.time())),
        ], limit=1)
        if not attendance:
            raise UserError('You must punch in before starting a task.')
        return attendance

    def _check_no_active_task(self, employee_id):
        """Ensure no other task is in_progress for this employee."""
        active = self.sudo().search([
            ('employee_id', '=', employee_id),
            ('state', '=', 'in_progress'),
        ], limit=1)
        if active:
            raise UserError(f'You already have an active task: {active.name}. End it first.')

    def action_start(self):
        """Start a task - called from API."""
        self.ensure_one()
        self._check_punch_in(self.employee_id.id)
        # self._check_no_active_task(self.employee_id.id)
        self.write({
            'state': 'in_progress',
            'start_time': datetime.now(),
        })

    @api.model
    def _cron_inactivity_check(self):
        """Find members inactive 3+ days and notify their PLs."""
        from datetime import timedelta
        today = date.today()
        three_days_ago = today - timedelta(days=3)
        Employee = self.env['hr.employee'].sudo()
        Attendance = self.env['hr.attendance'].sudo()
        Notification = self.env['kubera.notification'].sudo()

        active_employees = Employee.search([('task_forge_active', '=', True)])
        for emp in active_employees:
            role = emp._get_task_forge_role()
            if role != 'tasker':
                continue
            recent = Attendance.search_count([
                ('employee_id', '=', emp.id),
                ('check_in', '>=', datetime.combine(three_days_ago, datetime.min.time())),
            ])
            if recent == 0 and emp.task_forge_pl_id and emp.task_forge_pl_id.user_id:
                Notification.create({
                    'title': 'Inactivity Alert',
                    'message': f'{emp.name} has been inactive for 3+ days.',
                    'user_id': emp.task_forge_pl_id.user_id.id,
                    'priority': '2',
                })

    def action_end(self, end_screenshot_url=None, blocker_reason=None):
        """End a task with either completion or blocker."""
        self.ensure_one()
        vals = {
            'end_time': datetime.now(),
        }
        if end_screenshot_url:
            vals['end_screenshot_url'] = end_screenshot_url
            vals['image_url_lines'] = [(0, 0, {'image_url': end_screenshot_url, 'image_type': 'end'})]

        if blocker_reason:
            vals['state'] = 'blocker'
            vals['blocker_reason'] = blocker_reason
            self.write(vals)
            # Create blocker record
            blocker = self.env['task.forge.blocker'].sudo().create({
                'name': blocker_reason[:100],
                'task_id': self.id,
                'employee_id': self.employee_id.id,
                'qr_id': self.employee_id.task_forge_qr_id.id if self.employee_id.task_forge_qr_id else False,
                'pl_id': self.employee_id.task_forge_pl_id.id if self.employee_id.task_forge_pl_id else False,
                'blocker_reason': blocker_reason,
                'state': 'pending',
            })
            # Notify QR
            if self.employee_id.task_forge_qr_id and self.employee_id.task_forge_qr_id.user_id:
                self.env['kubera.notification'].sudo().create({
                    'title': 'New Blocker Raised',
                    'message': f'{self.employee_id.name} reported a blocker on task "{self.name}": {blocker_reason[:200]}',
                    'user_id': self.employee_id.task_forge_qr_id.user_id.id,
                    'priority': '2',
                    'res_model': 'task.forge.blocker',
                    'res_id': blocker.id,
                })
            return blocker
        else:
            vals['state'] = 'completed'
            self.write(vals)
            return self

class TaskForgeImages(models.Model):
    _name = 'task.forge.image'

    image_url = fields.Char(string='Image URL')
    image_type = fields.Selection([('start', 'Start'), ('end', 'End')])
    task_id = fields.Many2one('task.forge.log', string='Task')
    status = fields.Selection([('draft', 'Draft'), ('rejected', 'Rejected'), ('approved', 'Approved')], default='draft')


