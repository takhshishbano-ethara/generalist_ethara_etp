# -*- coding: utf-8 -*-
import base64
import json

from odoo import _, fields, models
from odoo.exceptions import UserError


class DocumensoUploadWizard(models.TransientModel):
    _name = 'documenso.upload.wizard'
    _description = 'Documenso Upload Wizard'

    title = fields.Char(string='Title', required=True)
    file_data = fields.Binary(string='PDF File', required=True, attachment=False)
    filename = fields.Char(string='Filename')
    recipients_json = fields.Text(
        string='Recipients (JSON)',
        help='Optional JSON array, e.g. [{"name":"Jane","email":"j@x.com","role":"SIGNER"}]',
    )
    send_immediately = fields.Boolean(string='Send After Upload', default=False)

    def _parse_recipients(self):
        self.ensure_one()
        if not self.recipients_json:
            return None
        try:
            data = json.loads(self.recipients_json)
        except ValueError as exc:
            raise UserError(_("Recipients JSON is not valid: %s") % exc) from exc
        if not isinstance(data, list):
            raise UserError(_("Recipients JSON must be a list of objects."))
        return data

    def action_upload(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_("Please attach a PDF file."))
        Settings = self.env['res.config.settings'].sudo()
        client = Settings.get_client()
        content = base64.b64decode(self.file_data)
        content_type = 'application/pdf'
        result = client.upload_document(
            title=self.title,
            content=content,
            filename=self.filename or (self.title + '.pdf'),
            content_type=content_type,
            recipients=self._parse_recipients(),
            send=self.send_immediately,
        )
        document_id = result.get('documentId')
        Document = self.env['documenso.document']
        record = Document
        if document_id:
            record = Document._upsert_from_payload(client.get_document(document_id))
        message = _("Uploaded '%s' to Documenso.") % self.title
        if document_id:
            message += _(" Document ID: %s") % document_id
        if record:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Documenso Document'),
                'res_model': 'documenso.document',
                'view_mode': 'form',
                'res_id': record.id,
                'target': 'current',
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Documenso Upload'),
                'message': message,
                'sticky': False,
                'type': 'success',
            },
        }
