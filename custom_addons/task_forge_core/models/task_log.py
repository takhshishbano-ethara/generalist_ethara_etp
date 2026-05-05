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
    is_justification_required = fields.Boolean(related='project_id.is_justification_required')

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
    prompt_text = fields.Text(string='Prompt')
    justification_text = fields.Text(string='Justification')
    feedback_note = fields.Text(string='Feedback Note')

    grammar_checked = fields.Boolean(string='Grammar Checked', default=False, index=True)
    grammar_is_perfect = fields.Boolean(string='Grammar Perfect', default=False, index=True)
    prompt_error_percentage = fields.Float(string='Prompt Error %', default=0)
    justification_error_percentage = fields.Float(string='Justification Error %', default=0)
    prompt_issue_count = fields.Integer(string='Prompt Issues', default=0)
    justification_issue_count = fields.Integer(string='Justification Issues', default=0)
    total_grammar_issues = fields.Integer(string='Total Grammar Issues', default=0, index=True)
    prompt_corrected = fields.Text(string='Corrected Prompt')
    justification_corrected = fields.Text(string='Corrected Justification')

    prompt_grammar_count = fields.Integer(string='Prompt Grammar Errors', default=0)
    prompt_misspelling_count = fields.Integer(string='Prompt Misspelling Errors', default=0)
    prompt_punctuation_count = fields.Integer(string='Prompt Punctuation Errors', default=0)
    prompt_clarity_count = fields.Integer(string='Prompt Clarity Errors', default=0)
    prompt_typography_count = fields.Integer(string='Prompt Typography Errors', default=0)
    prompt_capitalization_count = fields.Integer(string='Prompt Capitalization Errors', default=0)
    prompt_miscellaneous_count = fields.Integer(string='Prompt Miscellaneous Errors', default=0)

    justification_grammar_count = fields.Integer(string='Justification Grammar Errors', default=0)
    justification_misspelling_count = fields.Integer(string='Justification Misspelling Errors', default=0)
    justification_punctuation_count = fields.Integer(string='Justification Punctuation Errors', default=0)
    justification_clarity_count = fields.Integer(string='Justification Clarity Errors', default=0)
    justification_typography_count = fields.Integer(string='Justification Typography Errors', default=0)
    justification_capitalization_count = fields.Integer(string='Justification Capitalization Errors', default=0)
    justification_miscellaneous_count = fields.Integer(string='Justification Miscellaneous Errors', default=0)

    blocker_ids = fields.One2many('task.forge.blocker', 'task_id', string='Blockers')
    bug_report_ids = fields.One2many('task.forge.bug.report', 'task_id', string='Bug Reports')
    rubric_rating_ids = fields.One2many(
        'task.forge.rubric.rating', 'log_id', string='Rubric Ratings',
    )
    rubric_completed = fields.Boolean(
        string='Rubric Completed',
        compute='_compute_rubric_completed',
        store=True,
    )

    response_ids = fields.One2many(
        'task.forge.response', 'task_id', string='Responses',
    )
    response_completed = fields.Boolean(
        string='Responses Completed',
        compute='_compute_response_completed',
        store=True,
    )

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

    @api.depends(
        'project_id', 'project_id.is_rubrics_required',
        'project_id.rubric_category_ids',
        'project_id.rubric_category_ids.dimension_ids',
        'project_id.rubric_category_ids.dimension_ids.is_required',
        'rubric_rating_ids', 'rubric_rating_ids.dimension_id',
    )
    def _compute_rubric_completed(self):
        for rec in self:
            project = rec.project_id
            if not project or not project.is_rubrics_required:
                rec.rubric_completed = True
                continue
            required_dims = self.env['rubric.dimension']
            for cat in project.rubric_category_ids:
                required_dims |= cat.dimension_ids.filtered(lambda d: d.is_required)
            if not required_dims:
                rec.rubric_completed = True
                continue
            rated_dim_ids = set(rec.rubric_rating_ids.mapped('dimension_id.id'))
            rec.rubric_completed = all(d.id in rated_dim_ids for d in required_dims)

    @api.depends(
        'project_id', 'project_id.is_response_required',
        'project_id.response_config_ids',
        'response_ids', 'response_ids.value',
    )
    def _compute_response_completed(self):
        for rec in self:
            project = rec.project_id
            if not project or not project.is_response_required:
                rec.response_completed = True
                continue
            config_count = len(project.response_config_ids)
            if not config_count:
                rec.response_completed = True
                continue
            filled = rec.response_ids.filtered(lambda r: r.value)
            rec.response_completed = len(filled) >= config_count

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
                    'project_id': self.project_id.id if self.project_id else False,
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


