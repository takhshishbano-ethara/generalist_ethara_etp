# -*- coding: utf-8 -*-
from odoo import api, fields, models


class DocumensoField(models.Model):
    _name = 'documenso.field'
    _description = 'Documenso Document Field'
    _order = 'page asc, id asc'

    document_id = fields.Many2one(
        'documenso.document', string='Document', required=True, ondelete='cascade', index=True)
    documenso_id = fields.Char(string='Documenso Field ID', required=True, index=True)
    field_type = fields.Char(string='Type')
    recipient_email = fields.Char(string='Recipient Email')
    page = fields.Integer(string='Page', default=1)
    inserted = fields.Boolean(string='Filled')
    value = fields.Text(string='Value')

    _sql_constraints = [
        ('field_uniq_per_document', 'unique(document_id, documenso_id)',
         'Field already exists for this document.'),
    ]

    @api.model
    def _payload_to_vals(self, payload, document_id):
        recipient = payload.get('recipient') if isinstance(payload.get('recipient'), dict) else {}
        return {
            'document_id': document_id,
            'documenso_id': str(payload.get('id') or ''),
            'field_type': payload.get('type') or payload.get('fieldType') or False,
            'recipient_email': recipient.get('email') or payload.get('recipientEmail') or False,
            'page': payload.get('page') or 1,
            'inserted': bool(payload.get('inserted')),
            'value': payload.get('customText') or payload.get('value') or False,
        }
