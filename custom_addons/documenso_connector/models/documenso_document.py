import base64
import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


DOCUMENT_STATUSES = [
    ('DRAFT', 'Draft'),
    ('PENDING', 'Pending'),
    ('COMPLETED', 'Completed'),
    ('REJECTED', 'Rejected'),
    ('EXPIRED', 'Expired'),
    ('OTHER', 'Other'),
]


class DocumensoDocument(models.Model):
    _name = 'documenso.document'
    _description = 'Documenso Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'documenso_created_at desc, id desc'
    _rec_name = 'title'

    documenso_id = fields.Char(string='Documenso ID', required=True, index=True, copy=False)
    external_id = fields.Char(string='External ID', index=True)
    title = fields.Char(string='Title', required=True, tracking=True)
    status = fields.Selection(DOCUMENT_STATUSES, string='Status', default='OTHER', tracking=True)
    source = fields.Char(string='Source')
    visibility = fields.Char(string='Visibility')
    user_email = fields.Char(string='Owner Email')

    documenso_created_at = fields.Datetime(string='Created On Documenso')
    documenso_updated_at = fields.Datetime(string='Updated On Documenso')
    documenso_completed_at = fields.Datetime(string='Completed On Documenso')

    recipient_ids = fields.One2many(
        'documenso.recipient', 'document_id', string='Recipients')
    field_ids = fields.One2many(
        'documenso.field', 'document_id', string='Fields')
    recipient_count = fields.Integer(compute='_compute_recipient_count', store=True)
    field_count = fields.Integer(compute='_compute_field_count', store=True)

    view_url = fields.Char(string='View URL', compute='_compute_view_url')
    download_url = fields.Char(string='Download URL', compute='_compute_download_url')
    download_binary = fields.Binary(string='Signed PDF', attachment=True, copy=False)
    download_filename = fields.Char(string='Signed PDF Filename')

    raw_payload = fields.Text(string='Raw Payload')
    last_synced_at = fields.Datetime(string='Last Synced', copy=False)

    _sql_constraints = [
        ('documenso_id_unique', 'unique(documenso_id)',
         'A Documenso document with this ID already exists.'),
    ]

    @api.depends('recipient_ids')
    def _compute_recipient_count(self):
        for record in self:
            record.recipient_count = len(record.recipient_ids)

    @api.depends('field_ids')
    def _compute_field_count(self):
        for record in self:
            record.field_count = len(record.field_ids)

    def _compute_view_url(self):
        web_url = self.env['ir.config_parameter'].sudo().get_param(
            'documenso_connector.documenso_web_url') or ''
        web_url = web_url.rstrip('/')
        for record in self:
            if web_url and record.documenso_id:
                record.view_url = '%s/documents/%s' % (web_url, record.documenso_id)
            else:
                record.view_url = False

    def _compute_download_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        base = base.rstrip('/')
        for record in self:
            if record.id and base:
                record.download_url = '%s/documenso/download/%s' % (base, record.id)
            else:
                record.download_url = False

    @api.model
    def _get_client(self):
        return self.env['res.config.settings'].sudo().get_client()

    @api.model
    def action_sync_all(self, page_size=None, cache_binaries=False):
        client = self._get_client()
        if not page_size:
            params = self.env['ir.config_parameter'].sudo()
            try:
                page_size = int(params.get_param('documenso_connector.documenso_page_size') or 25)
            except (TypeError, ValueError):
                page_size = 25
        created = updated = 0
        for payload in client.iter_documents(per_page=page_size):
            record, was_created = self._upsert_from_payload(payload)
            if was_created:
                created += 1
            else:
                updated += 1
            if cache_binaries and record and record.status == 'COMPLETED':
                try:
                    record._cache_download()
                except Exception:
                    _logger.exception("Failed to cache PDF for %s", record.documenso_id)
        _logger.info("Documenso sync finished: created=%s updated=%s", created, updated)
        return {'created': created, 'updated': updated}

    def action_sync_one(self):
        client = self._get_client()
        for record in self:
            if not record.documenso_id:
                continue
            payload = client.get_document_with_fields(record.documenso_id)
            record._upsert_from_payload(payload)
        return True

    def action_download_pdf(self):
        self.ensure_one()
        self._cache_download()
        if not self.download_binary:
            raise UserError(_("Documenso did not return a downloadable file."))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/documenso.document/%s/download_binary/%s?download=true' % (
                self.id, self.download_filename or ('%s.pdf' % self.documenso_id)),
            'target': 'self',
        }

    def action_open_view_url(self):
        self.ensure_one()
        if not self.view_url:
            raise UserError(_("View URL is not configured. Set Documenso Web URL in Settings."))
        return {
            'type': 'ir.actions.act_url',
            'url': self.view_url,
            'target': 'new',
        }

    def _cache_download(self):
        self.ensure_one()
        if self.download_binary:
            return
        client = self._get_client()
        result = client.download_document_pdf(self.documenso_id)
        if result.get('redirect_url'):
            return
        content = result.get('content')
        if not content:
            return
        filename = self.download_filename or ('%s.pdf' % (self.title or self.documenso_id))
        if not filename.lower().endswith('.pdf'):
            filename = '%s.pdf' % filename
        self.write({
            'download_binary': base64.b64encode(content),
            'download_filename': filename,
        })

    @api.model
    def _upsert_from_payload(self, payload):
        documenso_id = str(payload.get('id') or payload.get('documentId') or '')
        if not documenso_id:
            _logger.warning("Skipping Documenso payload without id: %s", payload)
            return self.browse(), False
        record = self.search([('documenso_id', '=', documenso_id)], limit=1)
        vals = self._payload_to_vals(payload)
        vals['last_synced_at'] = fields.Datetime.now()
        was_created = not bool(record)
        if record:
            record.write(vals)
        else:
            record = self.create(vals)
        record._sync_recipients(payload)
        record._sync_fields(payload)
        return record, was_created

    @api.model
    def _payload_to_vals(self, payload):
        return {
            'documenso_id': str(payload.get('id') or payload.get('documentId')),
            'external_id': payload.get('externalId') or payload.get('external_id') or False,
            'title': payload.get('title') or _('Untitled Documenso Document'),
            'status': self._map_status(payload.get('status')),
            'source': payload.get('source') or False,
            'visibility': payload.get('visibility') or False,
            'user_email': (payload.get('user') or {}).get('email') if isinstance(payload.get('user'), dict) else payload.get('userEmail') or False,
            'documenso_created_at': self._parse_datetime(payload.get('createdAt')),
            'documenso_updated_at': self._parse_datetime(payload.get('updatedAt')),
            'documenso_completed_at': self._parse_datetime(payload.get('completedAt')),
            'raw_payload': self._serialize_payload(payload),
        }

    def _sync_recipients(self, payload):
        recipients = payload.get('recipients') or payload.get('Recipient') or []
        if not isinstance(recipients, list):
            return
        Recipient = self.env['documenso.recipient']
        keep_ids = []
        for item in recipients:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get('id') or '')
            existing = self.recipient_ids.filtered(
                lambda r, eid=external_id: r.documenso_id == eid)
            vals = Recipient._payload_to_vals(item, self.id)
            if existing:
                existing.write(vals)
                keep_ids.append(existing.id)
            else:
                keep_ids.append(Recipient.create(vals).id)
        stale = self.recipient_ids.filtered(lambda r: r.id not in keep_ids)
        if stale:
            stale.unlink()

    def _sync_fields(self, payload):
        fields_payload = payload.get('fields') or payload.get('Field') or []
        if not isinstance(fields_payload, list):
            return
        Field = self.env['documenso.field']
        keep_ids = []
        for item in fields_payload:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get('id') or '')
            existing = self.field_ids.filtered(
                lambda f, eid=external_id: f.documenso_id == eid)
            vals = Field._payload_to_vals(item, self.id)
            if existing:
                existing.write(vals)
                keep_ids.append(existing.id)
            else:
                keep_ids.append(Field.create(vals).id)
        stale = self.field_ids.filtered(lambda f: f.id not in keep_ids)
        if stale:
            stale.unlink()

    @staticmethod
    def _map_status(value):
        if not value:
            return 'OTHER'
        upper = str(value).upper()
        allowed = {code for code, _ in DOCUMENT_STATUSES}
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

    @staticmethod
    def _serialize_payload(payload):
        try:
            import json
            return json.dumps(payload, default=str, indent=2)
        except (TypeError, ValueError):
            return str(payload)

    @api.model
    def _cron_sync_documents(self):
        try:
            self.action_sync_all()
        except Exception:
            _logger.exception("Documenso cron sync failed")
            raise
