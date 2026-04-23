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
    blocker_type = fields.Char('Blocker Type')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('no_issue', 'No Issue'),
        ('escalated_to_pl', 'Escalated to PL'),
        ('escalated_to_cto', 'Escalated to CTO'),
        ('resolved', 'Resolved'),
        ('validated', 'Validated as Bug'),
        # Keep old states for backward compatibility
        ('ack', 'Acknowledged'),
        ('escalated', 'Escalated'),
    ], string='Status', default='pending', tracking=True)

    # --- QR fields (existing) ---
    qr_notes = fields.Text(string='QR Notes')
    qr_video_url = fields.Char(string='QR Video URL')
    qr_image_url = fields.Char(string='QR Image URL')
    qr_action_at = fields.Datetime(string='QR Action Time')

    # --- PL fields ---
    pl_notes = fields.Text(string='PL Notes')
    pl_image_url = fields.Char(string='PL Image URL')
    pl_action_at = fields.Datetime(string='PL Action Time')
    pl_validated_at = fields.Datetime(string='PL Validated Time')

    # --- CTO fields ---
    cto_notes = fields.Text(string='CTO Notes')
    cto_image_url = fields.Char(string='CTO Image URL')
    cto_action_at = fields.Datetime(string='CTO Action Time')

    # --- Resolution ---
    resolved_by_id = fields.Many2one('hr.employee', string='Resolved By')
    resolved_at = fields.Datetime(string='Resolved Time')
    resolution_notes = fields.Text(string='Resolution Notes')

    # --- Escalation tracking ---
    escalation_level = fields.Selection([
        ('qr', 'QR'),
        ('pl', 'PL'),
        ('cto', 'CTO'),
    ], string='Current Level', default='qr')
    escalation_log_ids = fields.One2many('task.forge.blocker.escalation.log', 'blocker_id', string='Escalation History')

    validated_bug_id = fields.Many2one('task.forge.validated.bug', string='Validated Bug')
    blocker_image_url = fields.Char(string='Blocker Image URL')

    employee_name = fields.Char(related='employee_id.name', store=True)
    task_name = fields.Char(related='task_id.name', store=True)
    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Medium'),
        ('2', 'High'),
        ('3', 'Critical')
    ], default='1', string="Priority", tracking=True)

    def _log_escalation(self, from_role, to_role, action, notes='', image_url='', document_urls=None):
        """Create an escalation log entry."""
        self.env['task.forge.blocker.escalation.log'].sudo().create({
            'blocker_id': self.id,
            'from_role': from_role,
            'to_role': to_role,
            'action': action,
            'notes': notes,
            'image_url': image_url or '',
            'document_urls': ','.join(document_urls) if document_urls else '',
            'action_by_id': self.env.user.employee_id.id if self.env.user.employee_id else False,
        })

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
        self._log_escalation('qr', 'tasker', 'no_issue', notes=notes or '')
        self.task_id.write({'state': 'returned'})

        if self.employee_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Returned',
                'message': 'Your blocker on "%s" was marked as No Issue by QR.' % self.task_id.name,
                'user_id': self.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
                'project_id': self.project_id.id if self.project_id else False,
            })

    def action_qr_escalate(self, notes=None, video_url=None, image_url=None, document_urls=None):
        """QR escalates blocker to PL."""
        self.ensure_one()
        vals = {
            'state': 'escalated_to_pl',
            'escalation_level': 'pl',
            'qr_action_at': datetime.now(),
        }
        if notes:
            vals['qr_notes'] = notes
        if video_url:
            vals['qr_video_url'] = video_url
        if image_url:
            vals['qr_image_url'] = image_url
        self.write(vals)
        self._log_escalation('qr', 'pl', 'escalate', notes=notes or '', image_url=image_url or '', document_urls=document_urls)
        self.task_id.write({'state': 'ack'})

        if self.pl_id and self.pl_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Escalated to PL',
                'message': 'QR escalated blocker "%s" on task "%s". Requires your action.' % (self.name, self.task_id.name),
                'user_id': self.pl_id.user_id.id,
                'priority': '2',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
                'project_id': self.project_id.id if self.project_id else False,
            })

    def action_pl_resolve(self, notes=None, image_url=None, document_urls=None):
        """PL resolves the blocker."""
        self.ensure_one()
        employee = self.env.user.employee_id
        vals = {
            'state': 'resolved',
            'pl_action_at': datetime.now(),
            'resolved_by_id': employee.id if employee else False,
            'resolved_at': datetime.now(),
            'resolution_notes': notes or '',
        }
        if notes:
            vals['pl_notes'] = notes
        if image_url:
            vals['pl_image_url'] = image_url
        self.write(vals)
        self._log_escalation('pl', '', 'resolve', notes=notes or '', image_url=image_url or '', document_urls=document_urls)
        self.task_id.write({'state': 'in_progress'})

        if self.employee_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Resolved by PL',
                'message': 'Your blocker "%s" has been resolved by PL.' % self.name,
                'user_id': self.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
                'project_id': self.project_id.id if self.project_id else False,
            })

    def action_pl_escalate_to_cto(self, notes=None, image_url=None, document_urls=None):
        """PL escalates blocker to CTO."""
        self.ensure_one()
        vals = {
            'state': 'escalated_to_cto',
            'escalation_level': 'cto',
            'pl_action_at': datetime.now(),
        }
        if notes:
            vals['pl_notes'] = notes
        if image_url:
            vals['pl_image_url'] = image_url
        self.write(vals)
        self._log_escalation('pl', 'cto', 'escalate', notes=notes or '', image_url=image_url or '', document_urls=document_urls)
        self.task_id.write({'state': 'escalated'})

        cto_role = self.env.ref('api_auth_gateway.role_cto_technical', raise_if_not_found=False)
        if cto_role:
            cto_users = self.env['res.users'].sudo().search([('user_role', '=', cto_role.id)], limit=5)
            for cto_user in cto_users:
                self.env['kubera.notification'].sudo().create({
                    'title': 'Blocker Escalated to CTO',
                    'message': 'PL escalated blocker "%s" on project "%s". Requires CTO action.' % (
                        self.name, self.project_id.name if self.project_id else ''),
                    'user_id': cto_user.id,
                    'priority': '3',
                    'res_model': 'task.forge.blocker',
                    'res_id': self.id,
                    'project_id': self.project_id.id if self.project_id else False,
                })

    def action_cto_resolve(self, notes=None, image_url=None, document_urls=None):
        """CTO resolves the blocker."""
        self.ensure_one()
        employee = self.env.user.employee_id
        vals = {
            'state': 'resolved',
            'cto_action_at': datetime.now(),
            'resolved_by_id': employee.id if employee else False,
            'resolved_at': datetime.now(),
            'resolution_notes': notes or '',
        }
        if notes:
            vals['cto_notes'] = notes
        if image_url:
            vals['cto_image_url'] = image_url
        self.write(vals)
        self._log_escalation('cto', '', 'resolve', notes=notes or '', image_url=image_url or '', document_urls=document_urls)
        self.task_id.write({'state': 'in_progress'})

        if self.employee_id.user_id:
            self.env['kubera.notification'].sudo().create({
                'title': 'Blocker Resolved by CTO',
                'message': 'Your blocker "%s" has been resolved by CTO.' % self.name,
                'user_id': self.employee_id.user_id.id,
                'priority': '1',
                'res_model': 'task.forge.blocker',
                'res_id': self.id,
                'project_id': self.project_id.id if self.project_id else False,
            })

    def action_cto_validate_bug(self, bug_data, notes=None, image_url=None, document_urls=None):
        """CTO validates blocker as a formal bug."""
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

        vals = {
            'state': 'validated',
            'cto_action_at': datetime.now(),
            'validated_bug_id': bug.id,
        }
        if notes:
            vals['cto_notes'] = notes
        if image_url:
            vals['cto_image_url'] = image_url
        self.write(vals)
        self._log_escalation('cto', '', 'validate_bug', notes=notes or '', image_url=image_url or '', document_urls=document_urls)
        self.task_id.write({'state': 'escalated'})

        return bug

    # Keep old method for backward compatibility
    def action_pl_validate(self, bug_data):
        """PL validates blocker into formal bug (legacy)."""
        return self.action_cto_validate_bug(bug_data)


class TaskForgeBlockerEscalationLog(models.Model):
    _name = 'task.forge.blocker.escalation.log'
    _description = 'Blocker Escalation Log'
    _order = 'create_date desc'

    blocker_id = fields.Many2one('task.forge.blocker', string='Blocker', required=True, ondelete='cascade')
    from_role = fields.Selection([
        ('tasker', 'Tasker'),
        ('qr', 'QR'),
        ('pl', 'PL'),
        ('cto', 'CTO'),
    ], string='From')
    to_role = fields.Selection([
        ('tasker', 'Tasker'),
        ('qr', 'QR'),
        ('pl', 'PL'),
        ('cto', 'CTO'),
        ('', 'Resolved'),
    ], string='To')
    action = fields.Selection([
        ('create', 'Created'),
        ('no_issue', 'Marked No Issue'),
        ('escalate', 'Escalated'),
        ('resolve', 'Resolved'),
        ('validate_bug', 'Validated as Bug'),
    ], string='Action')
    notes = fields.Text(string='Notes')
    image_url = fields.Char(string='Image URL')
    document_urls = fields.Text(string='Document URLs')
    action_by_id = fields.Many2one('hr.employee', string='Action By')
    action_by_name = fields.Char(related='action_by_id.name', store=True)
