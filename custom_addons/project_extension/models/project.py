from odoo import models, fields, api


class Project(models.Model):
    _inherit = 'project.project'

    project_attachments = fields.Many2many('ir.attachment', 'project_project_hr_employee_attachment_rel', string='Attachments')
    internal_project_name = fields.Char(string='Internal Project')
    client_name = fields.Char(string='Client Name')
    internal_client_name = fields.Char(string='Internal Client Name')
    # project_category = fields.Many2one('project.category', string='Project')
    project_category = fields.Selection([('technical', 'Technical'), ('stem', 'Stem'), ('non_stem', 'Non Stem')], string='Project Category')
    project_type = fields.Selection([('single_turn', 'Single Turn'), ('multi_turn', 'Multi Turn')], default='single_turn')
    sample_task_number = fields.Integer(string='Sample Task Number')
    # project_lead = fields.Many2many('res.users','project_project_res_users_lead_rel','project_id','user_id', string='Lead')
    # project_aire = fields.Many2many('res.users','project_project_res_users_aire_rel', 'project_id','user_id', string='AI Research Engineer (AIRE)')
    # project_swe = fields.Many2many('res.users','project_project_res_users_swe_rel','project_id','user_id', string='Software Engineer (SWE)')
    project_lead = fields.Many2many('hr.employee','project_project_hr_employee_lead_rel', string='Lead')
    project_aire = fields.Many2many('hr.employee','project_project_hr_employee_aire_rel', string='AI Research Engineer (AIRE)')
    project_swe = fields.Many2many('hr.employee','project_project_hr_employee_swe_rel', string='Software Engineer (SWE)')
    ai_generated_description = fields.Text(string='AI Generated Description')
    # Whatsapp Group
    whatsapp_group_name = fields.Char(string='Whatsapp Group Name')
    whatsapp_group_members = fields.Many2many('whatsapp.group.members', string='Whatsapp Group Members')
    # Slack Group
    slack_channel_name = fields.Char(string='Slack Channel Name')
    slack_members = fields.Many2many('hr.employee', 'project_project_hr_employee_slack_member_rel', string='Slack Members')
    slack_channel_id = fields.Many2one('discuss.channel', string="Slack Channel")
    # Google Drive
    google_drive_id = fields.Many2one('google.drive.file', string='Google Drive ID')
    # kick off email
    kick_off_to_mails = fields.Char(string='Kick Off To Mails')
    kick_off_subject = fields.Char(string='Kick Off Subject')
    kick_off_body = fields.Text('Kick Off Body')

    def create_slack_channel(self):
        user_ids = []
        user_ids.extend(self.project_lead.mapped('user_id'))
        user_ids.extend(self.project_aire.mapped('user_id'))
        user_ids.extend(self.project_swe.mapped('user_id'))
        result = self.env['discuss.channel'].sudo().create_slack_channel(
            channel_name=self.slack_channel_name,
            admin_id=user_ids[0].id if user_ids else None,
            user_ids=[uid.id for uid in user_ids if uid],
        )

        if result.get('success'):
            self.slack_channel_id = result.get('channel_id')

    def kick_off_send_mail(self):
        outgoing_server_name = self.env['ir.mail_server'].sudo().search([], limit=1).name or "atech@yopmail.com"
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

    def create_google_drive_folders(self):
        self.ensure_one()
        parent_wizard = self.env['google.drive.wizard'].create({
            'name': self.name,
            'upload_type': 'folder'
        })
        parent_folder = parent_wizard._create_folder()

        if parent_folder:
            self.google_drive_id = parent_folder.id
            for sub in ["Internal", "External"]:
                drive_record = self.env['google.drive.wizard'].create({
                    'name': sub,
                    'upload_type': 'folder',
                    'parent_folder_id': parent_folder.id
                })._create_folder()
                if self.project_attachments:
                    self.upload_attachments_in_drive(drive_id = drive_record)

    def upload_attachments_in_drive(self, drive_id=None):
        import base64
        for attach in self.project_attachments:
            drive_record = self.env['google.drive.wizard'].create({
                'name': attach.name,
                'upload_type': 'file',
                'file_content': attach.datas,
                'parent_folder_id': drive_id.id if drive_id else None,
            })._upload_file()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        projects = super(Project, self).create(vals_list)
        for project in projects:
            project.kick_off_send_mail()
            project.create_google_drive_folders()
            try:
                project.create_slack_channel()
            except Exception as e:
                print(f"{e}")
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


class WhatsappGroupMembers(models.Model):
    _name = 'whatsapp.group.members'
    _description = 'Whatsapp Group Members'

    name = fields.Char(string='Name')
    email = fields.Char(string='Email')
    country_code = fields.Char(string='Country Code')
    phone_number = fields.Char(string='Phone Number')
