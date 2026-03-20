import base64
import json
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class GoogleDriveWizard(models.TransientModel):
    _name = 'google.drive.wizard'
    _description = 'Upload to Google Drive'

    name = fields.Char(string="Name / Folder Name", required=True)
    upload_type = fields.Selection([
        ('folder', 'Create Folder'),
        ('file', 'Upload File')
    ], default='file')
    file_content = fields.Binary(string="Select File")
    parent_folder_id = fields.Many2one('google.drive.file', string="In Folder", domain="[('type', '=', 'folder')]")

    def _check_token_and_request(self, url, method='POST', json_data=None, files=None):
        """ Helper to handle 401 Unauthorized by refreshing the token """
        company = self.env.company
        headers = {'Authorization': f'Bearer {company.hangout_company_access_token}'}

        # Determine request type
        if files:
            resp = requests.post(url, headers=headers, files=files, timeout=20)
        elif method == 'POST':
            resp = requests.post(url, headers=headers, json=json_data, timeout=20)
        else:
            resp = requests.get(url, headers=headers, timeout=20)

        # If 401, refresh and retry once
        if resp.status_code == 401:
            if hasattr(company, 'google_meet_company_refresh_token'):
                company.google_meet_company_refresh_token()
                # Update header with new token
                headers['Authorization'] = f'Bearer {company.hangout_company_access_token}'
                if files:
                    resp = requests.post(url, headers=headers, files=files, timeout=20)
                else:
                    resp = requests.post(url, headers=headers, json=json_data, timeout=20)
            else:
                raise UserError(_("Session expired. Please re-authenticate in Settings."))

        return resp

    def action_confirm(self):
        if self.upload_type == 'folder':
            self._create_folder()
        else:
            if not self.file_content:
                raise UserError(_("Please select a file to upload."))
            self._upload_file()

        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _create_folder(self):
        url = "https://www.googleapis.com/drive/v3/files"
        metadata = {
            'name': self.name,
            'mimeType': 'application/vnd.google-apps.folder',
        }
        if self.parent_folder_id:
            metadata['parents'] = [self.parent_folder_id.google_id]

        resp = self._check_token_and_request(url, json_data=metadata)

        if resp.status_code == 200:
            data = resp.json()
            obj_file_id = self.env['google.drive.file'].create({
                'name': self.name,
                'google_id': data['id'],
                'type': 'folder',
                'parent_id': self.parent_folder_id.id,
                'url': f"https://drive.google.com/drive/folders/{data['id']}"
            })
            return obj_file_id
        else:
            raise UserError(_("Google Drive Error: %s") % resp.text)

    def _upload_file(self):
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        metadata = {'name': self.name}
        if self.parent_folder_id:
            metadata['parents'] = [self.parent_folder_id.google_id]

        files = {
            'data': ('metadata', json.dumps(metadata), 'application/json; charset=UTF-8'),
            'file': base64.b64decode(self.file_content)
        }

        resp = self._check_token_and_request(url, files=files)

        if resp.status_code == 200:
            data = resp.json()
            obj_file_id = self.env['google.drive.file'].create({
                'name': self.name,
                'google_id': data['id'],
                'type': 'file',
                'parent_id': self.parent_folder_id.id,
                'url': f"https://drive.google.com/file/d/{data['id']}/view"
            })
            return obj_file_id
        else:
            raise UserError(_("Google Drive Error: %s") % resp.text)
