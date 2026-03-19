from odoo import models, fields, api


class Project(models.Model):
    _inherit = 'project.project'

    project_attachments = fields.One2many('project.attachment', 'project_id', string='Attachments')
    internal_project_name = fields.Char(string='Internal Project')
    client_name = fields.Char(string='Client Name')
    internal_client_name = fields.Char(string='Internal Client Name')
    project_category = fields.Many2one('project.category', string='Project')
    project_type = fields.Selection([('single_turn', 'Single Turn'), ('multi_turn', 'Multi Turn')], default='single_turn')
    sample_task_number = fields.Integer(string='Sample Task Number')
    # project_lead = fields.Many2many('res.users','project_project_res_users_lead_rel','project_id','user_id', string='Lead')
    # project_aire = fields.Many2many('res.users','project_project_res_users_aire_rel', 'project_id','user_id', string='AI Research Engineer (AIRE)')
    # project_swe = fields.Many2many('res.users','project_project_res_users_swe_rel','project_id','user_id', string='Software Engineer (SWE)')
    project_lead = fields.Many2many('hr.employee','project_project_hr_employee_lead_rel','project_id','user_id', string='Lead')
    project_aire = fields.Many2many('hr.employee','project_project_hr_employee_aire_rel', 'project_id','user_id', string='AI Research Engineer (AIRE)')
    project_swe = fields.Many2many('hr.employee','project_project_hr_employee_swe_rel','project_id','user_id', string='Software Engineer (SWE)')
    ai_generated_description = fields.Text(string='AI Generated Description')
    # Whatsapp Group
    # Slack Group
    # Google Drive
    # kick off email
    kick_off_to_mails = fields.Char(string='Kick Off To Mails')
    kick_off_subject = fields.Char(string='Kick Off Subject')
    kick_off_body = fields.Text('Kick Off Body')

    def kick_off_send_mail(self):
        outgoing_server_name = self.env['ir.mail_server'].sudo().search([], limit=1).name
        for record in self:
            template = self.env.ref('project_extension.email_notification_template_view')
            email_values = {
                'email_from': outgoing_server_name,
                'email_to': record.kick_off_to_mails,
                'subject': record.kick_off_subject,
                'body_html': record.kick_off_body
            }
            template.sudo().send_mail(record.id, email_values=email_values, force_send=True)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        projects = super(Project, self).create(vals_list)
        projects.kick_off_send_mail()
        return projects

class ProjectAttachment(models.Model):
    _name = 'project.attachment'
    _description = 'Project Attachment'

    project_id = fields.Many2one('project.project', string='Project')
    image_url = fields.Char(string='Image URL')

class ProjectCategory(models.Model):
    _name = 'project.category'
    _description = 'Project Category'

    name = fields.Char(string='Name')
