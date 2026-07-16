import logging
import mimetypes
import os
import time
import uuid

from werkzeug.utils import secure_filename

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

S3_PREFIX = 'ethara_project'


class EtharaProjectAttachment(models.Model):
    _name = 'ethara.project.attachment'
    _description = 'Ethara Project Attachment'
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    project_id = fields.Many2one(
        comodel_name='ethara.project',
        string='Project',
        required=True,
        ondelete='cascade',
        index=True,
    )
    name = fields.Char(
        string='Name',
        required=True,
        help='Display name for the document (e.g. "SOW.pdf" or "Brief").',
    )
    attachment_url = fields.Char(
        string='Link',
        required=True,
        help='Public link to the document. For uploaded files this is set '
             'automatically to the S3 URL after upload.',
    )
    file_upload = fields.Binary(
        string='Upload File',
        attachment=False,
        help='Optional file upload. When present it is pushed to S3 and only '
             'the resulting URL is stored on this record; the binary is not '
             'persisted in the database.',
    )
    file_name = fields.Char(string='File Name')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._process_upload(vals)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('file_upload'):
            self._process_upload(vals)
        return super().write(vals)

    @api.constrains('attachment_url')
    def _check_url(self):
        for rec in self:
            if not rec.attachment_url:
                raise ValidationError(_('Attachment link is required.'))

    @api.model
    def _process_upload(self, vals):
        binary = vals.get('file_upload')
        if not binary:
            return

        connector = self.env['s3.connector'].sudo().search([], limit=1)
        if not connector:
            raise UserError(_(
                'No S3 connector configured. Configure an s3.connector record '
                'before uploading files.'
            ))

        original_name = vals.get('file_name') or vals.get('name') or 'file'
        safe_name = secure_filename(original_name) or 'file'
        base, ext = os.path.splitext(safe_name)
        if not ext:
            mime, _enc = mimetypes.guess_type(safe_name)
            ext = mimetypes.guess_extension(mime) if mime else ''
            ext = ext or '.bin'
            safe_name = f"{base}{ext}"

        unique = f"{time.time_ns()}_{uuid.uuid4().hex[:12]}"
        object_key = f"{S3_PREFIX}/{unique}_{safe_name}"

        url = connector.sudo().upload_to_s3(binary, object_key)
        if not url:
            raise UserError(_('S3 upload returned an empty URL.'))

        vals['attachment_url'] = url
        vals['file_upload'] = False
        if not vals.get('name'):
            vals['name'] = original_name
        if not vals.get('file_name'):
            vals['file_name'] = original_name
