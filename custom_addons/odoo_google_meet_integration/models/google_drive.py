import json
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class GoogleDriveFile(models.Model):
    _name = 'google.drive.file'
    _description = 'Google Drive Files and Folders'
    _order = 'type desc, name'

    name = fields.Char(string="Name", required=True)
    google_id = fields.Char(string="Google Drive ID", readonly=True)
    parent_id = fields.Many2one('google.drive.file', string="Parent Folder", domain="[('type', '=', 'folder')]")
    type = fields.Selection([
        ('file', 'File'),
        ('folder', 'Folder')
    ], string="Type", default='file', required=True)
    url = fields.Char(string="View in Google Drive", readonly=True)

    def get_drive_data(self):
        vals = {
            'name': self.name or "",
            'type': self.type or "",
            'url': self.url or "",
            'sub_files': [i.get_drive_data() for i in self.env['google.drive.file'].sudo().search(
                [('parent_id', '=', self.id)])],
        }
        return vals

    def action_open_google_drive(self):
        return {
            'type': 'ir.actions.act_url',
            'url': self.url,
            'target': 'new',
        }

    def _get_google_auth_header(self):
        # Reusing your existing Google Meet/Hangout token logic
        company = self.env.company
        if not company.hangout_company_access_token:
            raise UserError(_("Please authenticate Google in Settings first."))
        return {
            'Authorization': f'Bearer {company.hangout_company_access_token}',
            'Content-Type': 'application/json'
        }
