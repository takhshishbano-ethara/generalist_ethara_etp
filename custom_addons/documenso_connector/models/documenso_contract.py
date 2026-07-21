# -*- coding: utf-8 -*-
import base64
import json
import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .documenso_client import build_prefill_fields, map_applicant_fields

_logger = logging.getLogger(__name__)

CONTRACT_STATUSES = [
    ('DRAFT', 'Draft'),
    ('SENT', 'Sent'),
    ('OPENED', 'Opened'),
    ('SIGNED', 'Signed'),
    ('REJECTED', 'Rejected'),
    ('CANCELLED', 'Cancelled'),
    ('EXPIRED', 'Expired'),
]

TERMINAL_STATUSES = {'SIGNED', 'REJECTED', 'CANCELLED', 'EXPIRED'}


class DocumensoContract(models.Model):
    _name = 'documenso.contract'
    _description = 'Documenso Employee Contract'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sent_at desc, id desc'
    _rec_name = 'name'

    name = fields.Char(string='Reference', required=True, default=lambda self: _('New'), copy=False)
    applicant_id = fields.Many2one(
        'hr.applicant', string='Applicant', required=True, ondelete='restrict', index=True, tracking=True)
    applicant_email = fields.Char(related='applicant_id.email_from', store=True, string='Applicant Email')
    recipient_name = fields.Char(
        string='Recipient', compute='_compute_recipient', store=True)
    recipient_email = fields.Char(
        string='Recipient Email', compute='_compute_recipient', store=True)
    job_id = fields.Many2one(
        'hr.job', string='Job', ondelete='set null', index=True, tracking=True)

    template_id = fields.Many2one(
        'documenso.template', string='Primary Template', ondelete='set null', tracking=True)
    template_ids = fields.Many2many(
        'documenso.template', 'documenso_contract_template_rel',
        'contract_id', 'template_id', string='Templates')
    doc_class = fields.Selection([
        ('contract', 'Contract'),
        ('compliance', 'Compliance'),
    ], string='Document Class', default='contract', required=True, index=True, tracking=True)

    documenso_id = fields.Char(string='Documenso Document ID', index=True, copy=False, tracking=True)
    envelope_id = fields.Char(string='Envelope ID', index=True, copy=False)
    status = fields.Selection(CONTRACT_STATUSES, string='Status', default='DRAFT', required=True,
                              tracking=True, index=True)
    signing_url = fields.Char(string='Signing URL', copy=False)

    sent_documents = fields.Text(string='Sent Bundle (JSON)', copy=False)

    pdf_binary = fields.Binary(string='Signed PDF', attachment=True, copy=False)
    pdf_filename = fields.Char(string='Signed PDF Filename', copy=False)

    item_ids = fields.One2many('documenso.contract.item', 'contract_id', string='Envelope Items')
    field_ids = fields.One2many('documenso.contract.field', 'contract_id', string='Extracted Fields')
    item_count = fields.Integer(compute='_compute_item_count', store=True)
    field_count = fields.Integer(compute='_compute_field_count', store=True)

    sent_at = fields.Datetime(string='Sent At', copy=False)
    signed_at = fields.Datetime(string='Signed At', copy=False)
    last_synced_at = fields.Datetime(string='Last Synced', copy=False)

    raw_payload = fields.Text(string='Raw Payload')
    note = fields.Text(string='Internal Note')

    _sql_constraints = [
        ('documenso_contract_doc_id_unique', 'unique(documenso_id)',
         'A contract with this Documenso document ID already exists.'),
    ]

    @api.depends('item_ids')
    def _compute_item_count(self):
        for record in self:
            record.item_count = len(record.item_ids)

    @api.depends('field_ids')
    def _compute_field_count(self):
        for record in self:
            record.field_count = len(record.field_ids)

    @api.depends('applicant_id', 'applicant_id.partner_name', 'applicant_id.email_from')
    def _compute_recipient(self):
        for record in self:
            record.recipient_name = record.applicant_id.partner_name or ''
            record.recipient_email = record.applicant_id.email_from or ''

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('documenso.contract') or _('New')
        return super().create(vals_list)

    @api.model
    def _get_client(self):
        return self.env['res.config.settings'].sudo().get_client()

    def action_send(self, template_ids=None, distribute=True, title_override=None,
                    extra_prefill=None):
        self.ensure_one()
        if self.status == 'SIGNED':
            raise UserError(_("Contract is already signed and cannot be re-sent."))
        if self.status == 'SENT' and self.documenso_id:
            raise UserError(_("Contract has already been sent (Documenso ID: %s).") % self.documenso_id)

        client = self._get_client()
        templates = self.env['documenso.template'].browse(template_ids) if template_ids else self.template_ids
        if not templates:
            if self.template_id:
                templates = self.template_id
            else:
                raise UserError(_("Select at least one template to send."))

        base_values = map_applicant_fields(self.applicant_id)
        if extra_prefill:
            base_values.update({str(k).lower(): v for k, v in extra_prefill.items() if v})

        primary_id = None
        primary_signing_url = None
        primary_envelope = None
        bundle = []

        for index, template in enumerate(templates):
            values = dict(base_values)
            if template.salary_bracket:
                values['salary bracket'] = template.salary_bracket
                values['salary'] = template.salary_bracket
            recipients = self._build_recipients(template)
            prefill = build_prefill_fields(
                [self._field_to_payload(f) for f in template.field_ids],
                values,
            )
            payload = client.use_template(
                template_id=template.documenso_id,
                recipients=recipients,
                distribute_document=distribute,
                prefill_fields=prefill,
                title_override=title_override,
            )
            documenso_id = client.extract_document_id(payload) or ''
            recipient_payload = self._first_recipient(payload)
            token = client.extract_signing_token(recipient_payload)
            signing_url = client.build_signing_url(token)
            envelope_id = self._extract_envelope_id(payload)

            bundle.append({
                'documensoId': documenso_id,
                'templateId': template.documenso_id,
                'templateTitle': template.title,
                'signingUrl': signing_url,
                'envelopeId': envelope_id,
                'primary': index == 0,
                'sentAt': fields.Datetime.now().isoformat(),
            })
            if index == 0:
                primary_id = documenso_id
                primary_signing_url = signing_url
                primary_envelope = envelope_id

        if not primary_id:
            raise UserError(_("Documenso did not return a document ID."))

        self.write({
            'documenso_id': primary_id,
            'envelope_id': primary_envelope,
            'signing_url': primary_signing_url,
            'sent_documents': json.dumps(bundle, indent=2),
            'status': 'SENT',
            'sent_at': fields.Datetime.now(),
            'template_id': templates[0].id,
            'template_ids': [(6, 0, templates.ids)],
            'doc_class': templates[0].doc_class or 'contract',
        })
        self.message_post(body=_(
            "Contract sent to %(email)s using %(n)s template(s)."
        ) % {'email': self.recipient_email or self.recipient_name, 'n': len(templates)})
        return True

    def _build_recipients(self, template):
        name = self.recipient_name or ''
        email = self.recipient_email or ''
        if not email:
            raise UserError(_(
                "Recipient email is missing for %s."
            ) % (self.recipient_name or _('applicant')))

        def _valid(placeholder):
            try:
                return int(placeholder.documenso_id)
            except (TypeError, ValueError):
                return None

        valid_placeholders = [p for p in template.recipient_ids if _valid(p) is not None]
        if not valid_placeholders:
            client = self._get_client()
            resp = client.create_template_recipients(template.documenso_id, [{
                'name': name or _('Signer'),
                'email': email,
                'role': 'SIGNER',
                'signingOrder': 1,
            }])
            new_recipient_id = None
            if isinstance(resp, dict):
                created_list = resp.get('recipients') or []
                if created_list:
                    new_recipient_id = created_list[0].get('id')
            if new_recipient_id:
                client.create_template_fields(template.documenso_id, [{
                    'recipientId': new_recipient_id,
                    'type': 'SIGNATURE',
                    'pageNumber': 1,
                    'pageX': 60,
                    'pageY': 85,
                    'width': 30,
                    'height': 8,
                }])
            template.action_refresh_one()
            valid_placeholders = [p for p in template.recipient_ids if _valid(p) is not None]
            if not valid_placeholders:
                raise UserError(_(
                    "Documenso did not return any recipient after provisioning for template '%s'."
                ) % template.title)

        recipients = []
        for placeholder in valid_placeholders:
            recipients.append({
                'id': _valid(placeholder),
                'name': name or placeholder.name or '',
                'email': email or placeholder.email or '',
            })
        return recipients

    @staticmethod
    def _field_to_payload(field):
        return {
            'id': field.documenso_id,
            'label': field.label,
            'type': field.field_type,
        }

    @staticmethod
    def _first_recipient(payload):
        for key in ('recipients', 'Recipient'):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list) and value:
                return value[0]
        for key in ('document', 'data'):
            nested = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(nested, dict):
                found = DocumensoContract._first_recipient(nested)
                if found:
                    return found
        return {}

    @staticmethod
    def _extract_envelope_id(payload):
        if not isinstance(payload, dict):
            return None
        for key in ('envelopeId', 'envelope_id'):
            value = payload.get(key)
            if value:
                return str(value)
        for key in ('document', 'data', 'envelope'):
            nested = payload.get(key)
            if isinstance(nested, dict):
                found = DocumensoContract._extract_envelope_id(nested)
                if found:
                    return found
        return None

    def action_sync(self):
        client = self._get_client()
        for record in self:
            if not record.documenso_id:
                continue
            payload = client.get_document_with_fields(record.documenso_id)
            record._apply_document_payload(payload)
        return True

    def action_download_pdf(self):
        self.ensure_one()
        if not self.pdf_binary:
            self._download_and_store_pdf()
        if not self.pdf_binary:
            raise UserError(_("Signed PDF is not available yet."))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/documenso.contract/%s/pdf_binary/%s?download=true' % (
                self.id, self.pdf_filename or ('contract-%s.pdf' % self.id)),
            'target': 'self',
        }

    def action_open_signing_url(self):
        self.ensure_one()
        if not self.signing_url:
            raise UserError(_("No signing URL on this contract."))
        return {
            'type': 'ir.actions.act_url',
            'url': self.signing_url,
            'target': 'new',
        }

    def _download_and_store_pdf(self):
        self.ensure_one()
        if not self.documenso_id:
            return
        client = self._get_client()
        payload = client.download_document_pdf(self.documenso_id)
        if payload.get('redirect_url'):
            self.signing_url = self.signing_url or payload['redirect_url']
            return
        content = payload.get('content')
        if not content:
            return
        filename = self.pdf_filename or (
            'contract-%s.pdf' % (self.recipient_name or self.documenso_id)).replace(' ', '_')
        if not filename.lower().endswith('.pdf'):
            filename = '%s.pdf' % filename
        self.write({
            'pdf_binary': base64.b64encode(content),
            'pdf_filename': filename,
        })

    def _apply_document_payload(self, payload):
        self.ensure_one()
        if not isinstance(payload, dict):
            return
        status = self._map_status(payload.get('status'))
        vals = {
            'status': status,
            'raw_payload': self._serialize(payload),
            'last_synced_at': fields.Datetime.now(),
        }
        envelope_id = self._extract_envelope_id(payload)
        if envelope_id:
            vals['envelope_id'] = envelope_id
        completed_at = payload.get('completedAt') or payload.get('signedAt')
        if status == 'SIGNED':
            vals['signed_at'] = self._parse_datetime(completed_at) or fields.Datetime.now()
        self.write(vals)
        self._sync_extracted_fields(payload)
        if status == 'SIGNED':
            self._process_completion()

    def _process_completion(self):
        self.ensure_one()
        self._download_and_store_pdf()
        if self.envelope_id:
            self._sync_envelope_items()
        self.message_post(body=_("Document signed by %s.") % (
            self.recipient_email or self.recipient_name or _('recipient')))

    def _sync_envelope_items(self):
        self.ensure_one()
        client = self._get_client()
        envelope = client.get_envelope(self.envelope_id)
        items = envelope.get('envelopeItems') if isinstance(envelope, dict) else None
        if not isinstance(items, list):
            return
        Item = self.env['documenso.contract.item']
        keep_ids = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get('id') or '')
            if not item_id:
                continue
            existing = self.item_ids.filtered(lambda i, iid=item_id: i.item_id == iid)
            vals = {
                'contract_id': self.id,
                'item_id': item_id,
                'title': entry.get('title') or entry.get('name') or _('Item'),
                'sequence': entry.get('order') or 0,
                'category': self._categorize_item(entry.get('title') or ''),
            }
            download = client.download_envelope_item_pdf(item_id)
            content = download.get('content')
            if content:
                filename = ('%s.pdf' % (vals['title'] or item_id)).replace(' ', '_')
                vals['pdf_binary'] = base64.b64encode(content)
                vals['pdf_filename'] = filename
            elif download.get('redirect_url'):
                vals['redirect_url'] = download['redirect_url']
            if existing:
                existing.write(vals)
                keep_ids.extend(existing.ids)
            else:
                keep_ids.append(Item.create(vals).id)
        stale = self.item_ids.filtered(lambda i: i.id not in keep_ids)
        if stale:
            stale.unlink()

    @staticmethod
    def _categorize_item(title):
        lower = (title or '').lower()
        if 'nda' in lower:
            return 'nda'
        if 'offer' in lower:
            return 'offer_letter'
        if any(k in lower for k in ('employment', 'agreement', 'appointment', 'contract')):
            return 'employment_agreement'
        return 'other'

    def _sync_extracted_fields(self, payload):
        self.ensure_one()
        raw = payload.get('fields') or payload.get('Field') or []
        if not isinstance(raw, list):
            return
        Field = self.env['documenso.contract.field']
        keep_ids = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            documenso_id = str(item.get('id') or '')
            if not documenso_id:
                continue
            existing = self.field_ids.filtered(lambda f, fid=documenso_id: f.documenso_id == fid)
            recipient = item.get('recipient') if isinstance(item.get('recipient'), dict) else {}
            vals = {
                'contract_id': self.id,
                'documenso_id': documenso_id,
                'label': item.get('label') or item.get('name') or False,
                'field_type': (item.get('type') or item.get('fieldType') or '').upper() or False,
                'value': item.get('customText') or item.get('value') or False,
                'inserted': bool(item.get('inserted')),
                'recipient_email': recipient.get('email') or item.get('recipientEmail') or False,
                'page': item.get('page') or 1,
            }
            if existing:
                existing.write(vals)
                keep_ids.extend(existing.ids)
            else:
                keep_ids.append(Field.create(vals).id)
        stale = self.field_ids.filtered(lambda f: f.id not in keep_ids)
        if stale:
            stale.unlink()

    @staticmethod
    def _map_status(value):
        if not value:
            return 'DRAFT'
        upper = str(value).upper()
        aliases = {
            'COMPLETED': 'SIGNED',
            'PENDING': 'SENT',
            'CANCELED': 'CANCELLED',
        }
        upper = aliases.get(upper, upper)
        allowed = {code for code, _ in CONTRACT_STATUSES}
        return upper if upper in allowed else 'SENT'

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
    def _serialize(payload):
        try:
            return json.dumps(payload, default=str, indent=2)
        except (TypeError, ValueError):
            return str(payload)

    @api.model
    def _find_by_documenso_id(self, documenso_id):
        if not documenso_id:
            return self.browse()
        return self.search([('documenso_id', '=', str(documenso_id))], limit=1)

    @api.model
    def _process_webhook(self, event, document_payload):
        documenso_id = document_payload.get('id') or document_payload.get('documentId')
        contract = self._find_by_documenso_id(documenso_id)
        if not contract:
            _logger.info("Documenso webhook: no contract found for document %s", documenso_id)
            return False
        if contract.status == 'SIGNED' and event not in ('document.completed',):
            _logger.info("Documenso webhook: contract %s already signed, ignoring %s",
                         contract.id, event)
            return False
        contract._apply_document_payload(document_payload)
        return True

    @api.model
    def _cron_sync_completed(self):
        client = self._get_client()
        for payload in client.iter_documents(status='COMPLETED'):
            documenso_id = payload.get('id') or payload.get('documentId')
            contract = self._find_by_documenso_id(documenso_id)
            if not contract:
                continue
            full = client.get_document_with_fields(documenso_id)
            contract._apply_document_payload(full)


class DocumensoContractField(models.Model):
    _name = 'documenso.contract.field'
    _description = 'Documenso Contract Extracted Field'
    _order = 'page asc, id asc'

    contract_id = fields.Many2one(
        'documenso.contract', string='Contract', required=True, ondelete='cascade', index=True)
    documenso_id = fields.Char(string='Documenso Field ID', required=True, index=True)
    label = fields.Char(string='Label')
    field_type = fields.Char(string='Type')
    value = fields.Text(string='Value')
    inserted = fields.Boolean(string='Filled')
    recipient_email = fields.Char(string='Recipient Email')
    page = fields.Integer(string='Page', default=1)

    _sql_constraints = [
        ('contract_field_uniq', 'unique(contract_id, documenso_id)',
         'Field already exists for this contract.'),
    ]


class DocumensoContractItem(models.Model):
    _name = 'documenso.contract.item'
    _description = 'Documenso Contract Envelope Item'
    _order = 'sequence asc, id asc'

    contract_id = fields.Many2one(
        'documenso.contract', string='Contract', required=True, ondelete='cascade', index=True)
    item_id = fields.Char(string='Envelope Item ID', required=True, index=True)
    title = fields.Char(string='Title', required=True)
    category = fields.Selection([
        ('offer_letter', 'Offer Letter'),
        ('nda', 'NDA'),
        ('employment_agreement', 'Employment Agreement'),
        ('other', 'Other'),
    ], string='Category', default='other')
    sequence = fields.Integer(string='Order', default=0)
    pdf_binary = fields.Binary(string='PDF', attachment=True)
    pdf_filename = fields.Char(string='Filename')
    redirect_url = fields.Char(string='Redirect URL')

    _sql_constraints = [
        ('contract_item_uniq', 'unique(contract_id, item_id)',
         'Envelope item already exists for this contract.'),
    ]

    def action_download(self):
        self.ensure_one()
        if self.pdf_binary:
            return {
                'type': 'ir.actions.act_url',
                'url': '/web/content/documenso.contract.item/%s/pdf_binary/%s?download=true' % (
                    self.id, self.pdf_filename or ('item-%s.pdf' % self.id)),
                'target': 'self',
            }
        if self.redirect_url:
            return {
                'type': 'ir.actions.act_url',
                'url': self.redirect_url,
                'target': 'new',
            }
        raise UserError(_("No downloadable content on this item."))
