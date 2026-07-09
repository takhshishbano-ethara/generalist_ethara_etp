# -*- coding: utf-8 -*-
from datetime import datetime

from odoo import api, fields, models


RECIPIENT_SIGNING_STATUSES = [
    ('NOT_SIGNED', 'Not Signed'),
    ('SIGNED', 'Signed'),
    ('REJECTED', 'Rejected'),
    ('OPENED', 'Opened'),
    ('SENT', 'Sent'),
    ('OTHER', 'Other'),
]


class DocumensoRecipient(models.Model):
    _name = 'documenso.recipient'
    _description = 'Documenso Document Recipient'
    _order = 'signing_order asc, id asc'

    document_id = fields.Many2one(
        'documenso.document', string='Document', required=True, ondelete='cascade', index=True)
    documenso_id = fields.Char(string='Documenso Recipient ID', required=True, index=True)
    name = fields.Char(string='Name')
    email = fields.Char(string='Email', required=True)
    role = fields.Char(string='Role')
    signing_status = fields.Selection(RECIPIENT_SIGNING_STATUSES, string='Status', default='OTHER')
    signing_order = fields.Integer(string='Order', default=0)
    signed_at = fields.Datetime(string='Signed At')
    signing_url = fields.Char(string='Signing URL')

    _sql_constraints = [
        ('recipient_uniq_per_document', 'unique(document_id, documenso_id)',
         'Recipient already exists for this document.'),
    ]

    @api.model
    def _payload_to_vals(self, payload, document_id):
        return {
            'document_id': document_id,
            'documenso_id': str(payload.get('id') or ''),
            'name': payload.get('name') or False,
            'email': payload.get('email') or '',
            'role': payload.get('role') or False,
            'signing_status': self._map_status(payload.get('signingStatus') or payload.get('status')),
            'signing_order': payload.get('signingOrder') or payload.get('order') or 0,
            'signed_at': self._parse_datetime(payload.get('signedAt')),
            'signing_url': payload.get('signingUrl') or False,
        }

    @staticmethod
    def _map_status(value):
        if not value:
            return 'OTHER'
        upper = str(value).upper()
        allowed = {code for code, _ in RECIPIENT_SIGNING_STATUSES}
        return upper if upper in allowed else 'OTHER'

    @staticmethod
    def _parse_datetime(value):
        if not value:
            return False
        if isinstance(value, datetime):
            return fields.Datetime.to_string(value)
        try:
            normalized = str(value).replace('Z', '+00:00')
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(tz=None).replace(tzinfo=None)
            return fields.Datetime.to_string(parsed)
        except (TypeError, ValueError):
            return False
