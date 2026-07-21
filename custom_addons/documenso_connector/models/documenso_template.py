# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


VISIBILITY_SELECTION = [
    ('EVERYONE', 'Everyone'),
    ('MANAGER_AND_ABOVE', 'Manager and Above'),
    ('ADMIN', 'Admin'),
]

TEMPLATE_TYPE_SELECTION = [
    ('PRIVATE', 'Private'),
    ('PUBLIC', 'Public'),
]


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

    def action_publish(self):
        for record in self:
            record.is_published = True
        return True

    def action_unpublish(self):
        for record in self:
            record.is_published = False
        return True
    external_id = fields.Char(string='External ID')
    visibility = fields.Char(string='Visibility')
    template_type = fields.Selection(
        TEMPLATE_TYPE_SELECTION, string='Template Type')
    public_title = fields.Char(string='Public Title')
    public_description = fields.Char(string='Public Description')
    folder_id = fields.Char(string='Folder ID')
    user_email = fields.Char(string='Owner Email')
    active = fields.Boolean(string='Active', default=True)
    is_published = fields.Boolean(
        string='Published', default=False, index=True,
        help='When enabled, the template appears in the applicant / employee send wizards.')
    job_id = fields.Many2one(
        'hr.job', string='Applicable Job', ondelete='set null', index=True,
        help='Restrict this template to a specific job. Leave empty to make it available for all jobs.')
    salary_bracket = fields.Char(
        string='Salary Bracket',
        help='Salary bracket applied when this template is sent. Prefilled from the job but editable.')

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

    @api.onchange('job_id')
    def _onchange_job_id(self):
        for record in self:
            if record.job_id:
                record.salary_bracket = getattr(record.job_id, 'salary_bracket', '') or record.salary_bracket

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
            if isinstance(payload, dict):
                items = payload.get('data') or payload.get('templates') or payload.get('results')
                if isinstance(items, list) and items:
                    payload = items[0]
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
        template_type = payload.get('type') or payload.get('templateType') or False
        if template_type and template_type not in dict(TEMPLATE_TYPE_SELECTION):
            template_type = False
        return {
            'documenso_id': str(payload.get('id') or payload.get('templateId') or ''),
            'title': payload.get('title') or _('Untitled Template'),
            'external_id': payload.get('externalId') or payload.get('external_id') or False,
            'visibility': payload.get('visibility') or False,
            'template_type': template_type,
            'public_title': payload.get('publicTitle') or False,
            'public_description': payload.get('publicDescription') or False,
            'folder_id': payload.get('folderId') or False,
            'user_email': user.get('email') or payload.get('userEmail') or False,
            'documenso_created_at': self._parse_datetime(payload.get('createdAt')),
            'documenso_updated_at': self._parse_datetime(payload.get('updatedAt')),
        }

    def action_update_on_documenso(self):
        client = self._get_client()
        for record in self:
            if not record.documenso_id:
                raise UserError(_("Template %s has no Documenso ID.") % record.title)
            data = {
                'title': record.title or None,
                'externalId': record.external_id or None,
                'visibility': record.visibility or None,
                'type': record.template_type or None,
                'publicTitle': record.public_title or None,
                'publicDescription': record.public_description or None,
                'folderId': record.folder_id or None,
            }
            client.update_template(record.documenso_id, data=data)
            payload = client.get_template(record.documenso_id)
            record._upsert_from_full_payload(payload)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Documenso'),
                'message': _('Template updated on Documenso.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def _sync_fields(self, payload):
        self.ensure_one()
        raw_fields = payload.get('fields') or payload.get('Field') or []
        if not isinstance(raw_fields, list):
            return
        Field = self.env['documenso.template.field']
        keep_ids = []
        recipient_by_documenso = {
            r.documenso_id: r for r in self.recipient_ids
        }
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            external_id = str(item.get('id') or '')
            existing = self.field_ids.filtered(
                lambda f, eid=external_id: f.documenso_id == eid)
            vals = Field._payload_to_vals(item, self.id)
            recipient_documenso_id = str(
                item.get('recipientId') or item.get('recipient_id') or '')
            recipient = recipient_by_documenso.get(recipient_documenso_id)
            if recipient:
                vals['recipient_id'] = recipient.id
            if existing:
                existing.write(vals)
                keep_ids.extend(existing.ids)
            else:
                keep_ids.append(Field.create(vals).id)
        stale = self.field_ids.filtered(lambda f: f.id not in keep_ids)
        if stale:
            stale.unlink()

    def action_open_designer(self):
        self.ensure_one()
        if not self.documenso_id:
            raise UserError(_("Template has no Documenso ID. Sync it first."))
        web_url = (self.env['ir.config_parameter'].sudo()
                   .get_param('documenso_connector.web_url') or '').rstrip('/')
        if not web_url:
            raise UserError(_(
                "Documenso Web URL is not configured. Set it in Settings > Documenso."))
        editor_url = '%s/templates/%s/edit' % (web_url, self.documenso_id)
        return {
            'type': 'ir.actions.client',
            'tag': 'documenso_template_designer',
            'name': _('Edit in Documenso: %s') % self.title,
            'params': {
                'template_id': self.id,
                'documenso_id': self.documenso_id,
                'title': self.title,
                'editor_url': editor_url,
            },
            'target': 'fullscreen',
        }

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
    recipient_id = fields.Many2one(
        'documenso.template.recipient', string='Recipient', ondelete='set null')
    page_x = fields.Float(string='Position X (%)', digits=(6, 3), default=0.0)
    page_y = fields.Float(string='Position Y (%)', digits=(6, 3), default=0.0)
    page_width = fields.Float(string='Width (%)', digits=(6, 3), default=10.0)
    page_height = fields.Float(string='Height (%)', digits=(6, 3), default=5.0)

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
            'page': payload.get('page') or payload.get('pageNumber') or 1,
            'required': bool(payload.get('required')),
            'recipient_email': recipient.get('email') or payload.get('recipientEmail') or False,
            'page_x': float(payload.get('pageX') or payload.get('positionX') or 0.0),
            'page_y': float(payload.get('pageY') or payload.get('positionY') or 0.0),
            'page_width': float(payload.get('pageWidth') or payload.get('width') or 10.0),
            'page_height': float(payload.get('pageHeight') or payload.get('height') or 5.0),
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
