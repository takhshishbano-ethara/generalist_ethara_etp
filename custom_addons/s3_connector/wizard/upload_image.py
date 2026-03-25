from odoo import models, fields, api, _
from odoo.exceptions import UserError

class S3UploadWizard(models.TransientModel):
    _name = 's3.upload.wizard'
    _description = 'Upload Images to S3'

    s3_connector_id = fields.Many2one('s3.connector', string="S3 Bucket", required=True)
    upload_file = fields.Binary(string='Upload File')
    prefix = fields.Char(string="S3 Folder Prefix", help="Optional folder path in S3 bucket")
    file_name = fields.Char(string="File Name")
    file_url = fields.Char(string="File URL")

    def upload_images_in_s3_get_url(self):
        self.ensure_one()

        # Check for S3 connector
        if not self.s3_connector_id:
            raise UserError(_("Cannot upload image: S3 Connector is missing. Please configure an S3 Connector first."))

        # Upload file to S3
        image_path = self.file_name
        if self.prefix:
            image_path = f"{self.prefix}/{self.file_name}"
        url = self.s3_connector_id.sudo().upload_to_s3(self.upload_file,image_path)
        return url

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('s3_connector_id'):
                raise UserError("S3 Connector is missing. Please select a valid S3 Connector before proceeding.")
        res = super(S3UploadWizard, self).create(vals_list)
        return res

    def upload_images_to_s3(self):
        self.ensure_one()

        # Check for S3 connector
        if not self.s3_connector_id:
            raise UserError(_("Cannot upload image: S3 Connector is missing. Please configure an S3 Connector first."))

        # Upload file to S3
        url = self.s3_connector_id.sudo().upload_to_s3(
            self.upload_file,
            f"{self.prefix}/{self.file_name}"
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('File uploaded successfully to S3.'),
                'type': 'success',
                'sticky': False,
                'url': url
            }
        }
