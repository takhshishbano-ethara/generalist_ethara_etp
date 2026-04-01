from odoo import models, fields, api
from datetime import timedelta


class ProjectBlocker(models.Model):
    _name = 'project.blocker'
    _description = 'Project Task Blocker'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Blocker Name', required=True, tracking=True)

    state = fields.Selection([
        ('raised', 'Raised'),
        ('in_progress', 'In Progress'),
        ('testing', 'Testing'),
        ('resolved', 'Resolved'),
        ('cancelled', 'Cancelled')
    ], default='raised', string="Status", tracking=True)

    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Medium'),
        ('2', 'High'),
        ('3', 'Critical')
    ], default='1', string="Priority", tracking=True)

    project_id = fields.Many2one('project.project', string='Project')
    task_id = fields.Many2one('base.project.task', string='Task', domain="[('project_id', '=', project_id)]")

    employee_id = fields.Many2one('hr.employee', string='Raised By', default=lambda self: self.env.user.employee_id, required=True)

    assigned_employee_ids = fields.Many2many('hr.employee', string='Current Handlers', tracking=True)

    escalation_level = fields.Integer(string="Level", default=1)
    next_escalation_date = fields.Datetime(string="Next Escalation Deadline")


    @api.model_create_multi
    def create(self, values):
        for vals in values:
            team = []
            qc_r = self.env['project.team.allocation'].sudo().search([('employee_id', '=', vals.get('employee_id'))], limit=1)
            if qc_r:
                team.append(qc_r.id)
            if vals.get('project_id'):
                project = self.env['project.project'].browse(vals.get('project_id'))
                team.extend(project.project_lead.ids)
            if team:
                vals['assigned_employee_ids'] = [(6, 0, team)]
        return super().create(values)

    def _cron_escalate_blockers(self):
        now = fields.Datetime.now()
        expired_blockers = self.search([('state', 'in', ['raised', 'in_progress', 'testing']), ('next_escalation_date', '<=', now)])
        for blocker in expired_blockers:
            # 2. Get the unique managers of all currently assigned employees
            # .mapped('parent_id') returns a recordset of hr.employee
            if blocker.escalation_level == 1:
                ctos = self.env['hr.employee'].sudo().search([('designation_id', '=', self.env.ref('project_extension.designation_cto').id)], limit=1)
                hours = {'0': 24, '1': 12, '2': 4, '3': 2}.get(blocker.priority, 12)
                new_deadline = now + timedelta(hours=hours)

                blocker.write({
                    'assigned_employee_ids': [(6, 0, ctos.ids)],
                    'escalation_level': 2,
                    'next_escalation_date': new_deadline
                })
                auth_names = ", ".join(ctos.mapped('name'))
                blocker.message_post(
                    body=f"⚠️ **Escalation Level 2 Triggered**<br/>"
                         f"The issue was not resolved in time. Responsibility has shifted to: **{auth_names}**.",
                    subtype_xmlid="mail.mt_note"
                )
            elif blocker.escalation_level == 2:
                ctos = self.env['hr.employee'].sudo().search([('designation_id', '=', self.env.ref('project_extension.designation_founder').id)], limit=1)
                hours = {'0': 24, '1': 12, '2': 4, '3': 2}.get(blocker.priority, 12)
                new_deadline = now + timedelta(hours=hours)

                blocker.write({
                    'assigned_employee_ids': [(6, 0, ctos.ids)],
                    'escalation_level': 3,
                    'next_escalation_date': new_deadline
                })
                auth_names = ", ".join(ctos.mapped('name'))
                blocker.message_post(
                    body=f"⚠️ **Escalation Level 3 Triggered**<br/>"
                         f"The issue was not resolved in time. Responsibility has shifted to: **{auth_names}**.",
                    subtype_xmlid="mail.mt_note"
                )

            else:
                blocker.message_post(body="🚨 **Critical Alert:** This issue has reached the top of the hierarchy and requires immediate Founder intervention.")