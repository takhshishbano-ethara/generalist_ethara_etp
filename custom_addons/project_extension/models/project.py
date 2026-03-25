from odoo import models, fields, api
import requests
import json

class Project(models.Model):
    _inherit = 'project.project'

    project_seq = fields.Char(string='Sequence', default='New')
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
    slack_channels_id = fields.Char(string="Slack Channel")
    # Google Drive
    google_drive_id = fields.Many2one('google.drive.file', string='Google Drive ID')
    # kick off email
    kick_off_to_mails = fields.Char(string='Kick Off To Mails')
    kick_off_subject = fields.Char(string='Kick Off Subject')
    kick_off_body = fields.Text('Kick Off Body')

    def create_slack_channel(self):
        user_ids = []
        for i in self.slack_members:
            if i.user_id:
                user_ids.append(i.user_id.id)
        if user_ids:
            url = "https://etp.stage.ethara.ai/api/create_slack_channel"
            # url = "http://localhost:8069/api/create_slack_channel"
            headers = {
                'Content-Type': 'application/json'
            }

            payload = {
                "channel_name": f"{self.slack_channel_name}-{self.id}",
                "admin_id": user_ids[0],
                "user_ids": user_ids
            }

            try:
                response = requests.post(url, headers=headers, data=json.dumps(payload))
                if response.status_code == 200:
                    res_json = response.json()
                    channel_info = res_json.get('data', {})
                    channel_id = channel_info.get('slack_channel_id')
                    self.slack_channels_id = channel_id
                    print(f"Successfully retrieved Channel ID: {channel_id}")
                else:
                    print("Failed to get a successful response.")
            except Exception as err:
                print(f"An error occurred: {err}")

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
        for vals in vals_list:
            vals['project_seq'] = self.env['ir.sequence'].next_by_code('project.project')
        projects = super(Project, self).create(vals_list)
        for project in projects:
            try:
                project.kick_off_send_mail()
            except Exception as e:
                print(f"{e}")
            try:
                project.create_google_drive_folders()
            except Exception as e:
                print(f"{e}")
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


class ProjectProjectStage(models.Model):
    _inherit = 'project.project.stage'

    lable_name = fields.Char(string='Lable Name')

class WhatsappGroupMembers(models.Model):
    _name = 'whatsapp.group.members'
    _description = 'Whatsapp Group Members'

    name = fields.Char(string='Name')
    email = fields.Char(string='Email')
    country_code = fields.Char(string='Country Code')
    phone_number = fields.Char(string='Phone Number')
