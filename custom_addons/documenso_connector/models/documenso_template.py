# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class DocumensoTemplate(models.Model):
    _name = 'documenso.template'
    _description = 'Documenso Template Cache'
    _order = 'title asc, id asc'
    _rec_name = 'title'

    documenso_id = fields.Char(string='Documenso Template ID', required=True, index=True, copy=False)
    title = fields.Char(string='Title', required=True)
    doc_class = fields.Selection([
        ('contract', 'Contract'),
        ('compliance', 'Compliance'),
    ], string='Document Class', default='contract', required=True, index=True)
    external_id = fields.Char(string='External ID')
    visibility = fields.Char(string='Visibility')
    user_email = fields.Char(string='Owner Email')
    active = fields.Boolean(string='Active', default=True)

    documenso_created_at = fields.Datetime(string='Created On Documenso')
    documenso_updated_at = fields.Datetime(string='Updated On Documenso')
    last_synced_at = fields.Datetime(string='Last Synced', copy=False)

    field_ids = fields.One2many('documenso.template.field', 'template_id', string='Fields')
    recipient_ids = fields.One2many('documenso.template.recipient', 'template_id', string='Recipients')
    field_count = fields.Integer(compute='_compute_field_count', store=True)
    recipient_count = fields.Integer(compute='_compute_recipient_count', store=True)

    raw_payload = fields.Text(string='Raw Payload')

    _sql_constraints = [
        ('documenso_template_id_unique', 'unique(documenso_id)',
         'A Documenso template with this ID already exists.'),
    ]

    @api.depends('field_ids')
    def _compute_field_count(self):
        for record in self:
            record.field_count = len(record.field_ids)

    @api.depends('recipient_ids')
    def _compute_recipient_count(self):
        for record in self:
            record.recipient_count = len(record.recipient_ids)

    @api.model
    def _get_client(self):
        return self.env['res.config.settings'].sudo().get_client()

    @api.model
    def action_refresh_cache(self):
        client = self._get_client()
        params = self.env['ir.config_parameter'].sudo()
        try:
            per_page = int(params.get_param('documenso_connector.page_size') or 25)
        except (TypeError, ValueError):
            per_page = 25
        created = updated = 0
        seen_ids = set()
        for payload in client.iter_templates(per_page=per_page):
            record, was_created = self._upsert_from_list_payload(payload)
            seen_ids.add(record.id)
            if was_created:
                created += 1
            else:
                updated += 1
        for record in self.search([]):
            if record.id not in seen_ids and record.active:
                record.active = False
        _logger.info("Documenso template cache: created=%s updated=%s", created, updated)
        return {'created': created, 'updated': updated}

    def action_refresh_one(self):
        client = self._get_client()
        for record in self:
            payload = client.get_template(record.documenso_id)
            record._upsert_from_full_payload(payload)
        return True

    @api.model
    def _upsert_from_list_payload(self, payload):
        documenso_id = str(payload.get('id') or payload.get('templateId') or '')
        if not documenso_id:
            return self.browse(), False
        record = self.search([('documenso_id', '=', documenso_id)], limit=1)
        vals = self._payload_to_vals(payload)
        vals['last_synced_at'] = fields.Datetime.now()
        was_created = not bool(record)
        if record:
            record.write(vals)
        else:
            record = self.create(vals)
        client = self._get_client()
        full = client.get_template(documenso_id)
        record._sync_fields(full)
        record._sync_recipients(full)
        record.raw_payload = self._serialize(full)
        return record, was_created

    def _upsert_from_full_payload(self, payload):
        self.ensure_one()
        vals = self._payload_to_vals(payload)
        vals['last_synced_at'] = fields.Datetime.now()
        vals['raw_payload'] = self._serialize(payload)
        self.write(vals)
        self._sync_fields(payload)
        self._sync_recipients(payload)
        return self

    @api.model
    def _payload_to_vals(self, payload):
        user = payload.get('user') if isinstance(payload.get('user'), dict) else {}
        return {
            'documenso_id': str(payload.get('id') or payload.get('templateId')),
            'title': payload.get('title') or _('Untitled Template'),
            'external_id': payload.get('externalId') or payload.get('external_id') or False,
            'visibility': payload.get('visibility') or False,
            'user_email': user.get('email') or payload.get('userEmail') or False,
            'documenso_created_at': self._parse_datetime(payload.get('createdAt')),
            'documenso_updated_at': self._parse_datetime(payload.get('updatedAt')),
        }

    def _sync_fields(self, payload):
        self.ensure_one()
        raw_fields = payload.get('fields') or payload.get('Field') or []
        if not isinstance(raw_fields, list):
            return
        Field = self.env['documenso.template.field']
        keep_ids = []
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get('id') or '')
            existing = self.field_ids.filtered(
                lambda f, eid=external_id: f.documenso_id == eid)
            vals = Field._payload_to_vals(item, self.id)
            if existing:
                existing.write(vals)
                keep_ids.extend(existing.ids)
            else:
                keep_ids.append(Field.create(vals).id)
        stale = self.field_ids.filtered(lambda f: f.id not in keep_ids)
        if stale:
            stale.unlink()

    def _sync_recipients(self, payload):
        self.ensure_one()
        raw = payload.get('recipients') or payload.get('Recipient') or []
        if not isinstance(raw, list):
            return
        Recipient = self.env['documenso.template.recipient']
        keep_ids = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get('id') or '')
            existing = self.recipient_ids.filtered(
                lambda r, eid=external_id: r.documenso_id == eid)
            vals = Recipient._payload_to_vals(item, self.id)
            if existing:
                existing.write(vals)
                keep_ids.extend(existing.ids)
            else:
                keep_ids.append(Recipient.create(vals).id)
        stale = self.recipient_ids.filtered(lambda r: r.id not in keep_ids)
        if stale:
            stale.unlink()

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
    def _cron_refresh_templates(self):
        try:
            self.action_refresh_cache()
        except Exception:
            _logger.exception("Documenso template refresh failed")
            raise


class DocumensoTemplateField(models.Model):
    _name = 'documenso.template.field'
    _description = 'Documenso Template Field'
    _order = 'page asc, id asc'

    template_id = fields.Many2one(
        'documenso.template', string='Template', required=True, ondelete='cascade', index=True)
    documenso_id = fields.Char(string='Documenso Field ID', required=True, index=True)
    label = fields.Char(string='Label')
    field_type = fields.Char(string='Type')
    page = fields.Integer(string='Page', default=1)
    required = fields.Boolean(string='Required')
    recipient_email = fields.Char(string='Recipient Email')

    _sql_constraints = [
        ('template_field_uniq', 'unique(template_id, documenso_id)',
         'Field already exists for this template.'),
    ]

    @api.model
    def _payload_to_vals(self, payload, template_id):
        recipient = payload.get('recipient') if isinstance(payload.get('recipient'), dict) else {}
        return {
            'template_id': template_id,
            'documenso_id': str(payload.get('id') or ''),
            'label': payload.get('label') or payload.get('name') or False,
            'field_type': (payload.get('type') or payload.get('fieldType') or '').upper() or False,
            'page': payload.get('page') or 1,
            'required': bool(payload.get('required')),
            'recipient_email': recipient.get('email') or payload.get('recipientEmail') or False,
        }


class DocumensoTemplateRecipient(models.Model):
    _name = 'documenso.template.recipient'
    _description = 'Documenso Template Recipient'
    _order = 'signing_order asc, id asc'

    template_id = fields.Many2one(
        'documenso.template', string='Template', required=True, ondelete='cascade', index=True)
    documenso_id = fields.Char(string='Documenso Recipient ID', required=True, index=True)
    name = fields.Char(string='Placeholder Name')
    email = fields.Char(string='Placeholder Email')
    role = fields.Char(string='Role')
    signing_order = fields.Integer(string='Order', default=0)

    _sql_constraints = [
        ('template_recipient_uniq', 'unique(template_id, documenso_id)',
         'Recipient already exists for this template.'),
    ]

    @api.model
    def _payload_to_vals(self, payload, template_id):
        return {
            'template_id': template_id,
            'documenso_id': str(payload.get('id') or ''),
            'name': payload.get('name') or False,
            'email': payload.get('email') or False,
            'role': payload.get('role') or False,
            'signing_order': payload.get('signingOrder') or payload.get('order') or 0,
        }
