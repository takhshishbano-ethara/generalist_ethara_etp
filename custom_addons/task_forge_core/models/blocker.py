from odoo import models, fields, api
from datetime import datetime


class TaskForgeBlocker(models.Model):
    _name = 'task.forge.blocker'
    _description = 'Task Forge Blocker'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Summary', required=True, tracking=True)
    task_id = fields.Many2one('task.forge.log', string='Task', required=True, ondelete='cascade')
    project_id = fields.Many2one(
        related='task_id.project_id', string='Project', store=True,
    )
    employee_id = fields.Many2one('hr.employee', string='Raised By', required=True)
    qr_id = fields.Many2one('hr.employee', string='QR')
    pl_id = fields.Many2one('hr.employee', string='PL')
    blocker_reason = fields.Text(string='Blocker Reason')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('no_issue', 'No Issue'),
        ('ack', 'Acknowledged'),
        ('escalated', 'Escalated'),
    ], string='Status', default='pending', tracking=True)

    qr_notes = fields.Text(string='QR Notes')
    qr_video_url = fields.Char(string='QR Video URL')
    qr_image_url = fields.Char(string='QR Image URL')
    qr_action_at = fields.Datetime(string='QR Action Time')
    pl_validated_at = fields.Datetime(string='PL Validated Time')

    validated_bug_id = fields.Many2one('task.forge.validated.bug', string='Validated Bug')

    employee_name = fields.Char(related='employee_id.name', store=True)
    task_name = fields.Char(related='task_id.name', store=True)

    def action_qr_no_issue(self, notes=None):
        """QR marks blocker as No Issue - returns task to tasker."""
        self.ensure_one()
        vals = {
            'state': 'no_issue',
            'qr_action_at': datetime.now(),
        }
        if notes:
            vals['qr_notes'] = notes
        self.write(vals)

        # Return task to Returned state
        self.task_id.write({'state': 'returned'})

        # Notify tasker
        if self.employee_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Returned',
                'message': f'Your blocker on "{self.task_id.name}" was marked as No Issue by QR. Please re-attempt the task.',
                'user_id': self.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
            })

    def action_qr_escalate(self, notes=None, video_url=None, image_url=None):
        """QR escalates blocker to PL."""
        self.ensure_one()
        vals = {
            'state': 'ack',
            'qr_action_at': datetime.now(),
        }
        if notes:
            vals['qr_notes'] = notes
        if video_url:
            vals['qr_video_url'] = video_url
        if image_url:
            vals['qr_image_url'] = image_url
        self.write(vals)

        # Update task state
        self.task_id.write({'state': 'ack'})

        # Notify PL
        if self.pl_id and self.pl_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Escalated',
                'message': f'QR escalated blocker on "{self.task_id.name}" by {self.employee_id.name}. Requires your validation.',
                'user_id': self.pl_id.user_id.id,
                'priority': '2',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
            })

    def action_pl_validate(self, bug_data):
        """PL validates blocker into formal bug."""
        self.ensure_one()

        bug = self.env['task.forge.validated.bug'].sudo().create({
            'name': bug_data.get('bug_title', self.name),
            'blocker_id': self.id,
            'task_id': self.task_id.id,
            'project_id': self.project_id.id if self.project_id else False,
            'employee_id': self.employee_id.id,
            'qr_id': self.qr_id.id if self.qr_id else False,
            'pl_id': self.pl_id.id if self.pl_id else False,
            'validated_by_id': self.env.user.employee_id.id,
            'bug_description': bug_data.get('bug_description', ''),
            'steps_to_reproduce': bug_data.get('steps_to_reproduce', ''),
            'pages_affected': bug_data.get('pages_affected', ''),
            'impact': bug_data.get('impact', 'medium'),
            'impact_details': bug_data.get('impact_details', ''),
            'blocker_reason': self.blocker_reason,
            'qr_video_url': self.qr_video_url,
            'qr_image_url': self.qr_image_url,
        })

        self.write({
            'state': 'escalated',
            'pl_validated_at': datetime.now(),
            'validated_bug_id': bug.id,
        })

        # Update task
        self.task_id.write({'state': 'escalated'})

        return bug
