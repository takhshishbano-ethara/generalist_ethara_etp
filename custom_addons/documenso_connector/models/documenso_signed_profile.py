# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DOC_CLASSES = [
    ('contract', 'Contract'),
    ('compliance', 'Compliance'),
    ('offer', 'Offer'),
    ('other', 'Other'),
]

DEFAULT_OPEN_URL_TTL_SECONDS = 900


class DocumensoSignedProfile(models.Model):
    _name = 'documenso.signed.profile'
    _description = 'Documenso Signed Document Repository'
    _order = 'signed_at desc, id desc'
    _rec_name = 'title'

    title = fields.Char(string='Title', required=True, index=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', ondelete='restrict', index=True)
    employee_email = fields.Char(related='employee_id.work_email', store=True, string='Employee Email')
    applicant_id = fields.Many2one(
        'hr.applicant', string='Applicant', ondelete='restrict', index=True)
    applicant_email = fields.Char(related='applicant_id.email_from', store=True, string='Applicant Email')
    recipient_name = fields.Char(string='Recipient', compute='_compute_recipient', store=True)
    recipient_email = fields.Char(string='Recipient Email', compute='_compute_recipient', store=True)
    department_id = fields.Many2one(
        'hr.department', string='Department', compute='_compute_department', store=True, index=True)

    contract_id = fields.Many2one(
        'documenso.contract', string='Source Contract', ondelete='set null', index=True)
    template_id = fields.Many2one(
        'documenso.template', string='Template', ondelete='set null', index=True)
    template_documenso_id = fields.Char(string='Template Documenso ID', index=True)

    doc_class = fields.Selection(DOC_CLASSES, string='Document Class',
                                 default='contract', required=True, index=True)

    documenso_id = fields.Char(string='Documenso Document ID', required=True, index=True, copy=False)
    envelope_id = fields.Char(string='Envelope ID', index=True)
    envelope_item_id = fields.Char(string='Envelope Item ID', index=True)

    pdf_binary = fields.Binary(string='Signed PDF', attachment=True, copy=False)
    pdf_filename = fields.Char(string='Signed PDF Filename', copy=False)
    external_url = fields.Char(string='External URL', copy=False)

    signed_at = fields.Datetime(string='Signed At', index=True, copy=False)
    recorded_at = fields.Datetime(string='Recorded At', default=fields.Datetime.now, copy=False)

    fields_json = fields.Text(string='Extracted Fields (JSON)')
    raw_payload = fields.Text(string='Raw Payload')

    open_url_token = fields.Char(string='Open URL Token', copy=False, groups='hr.group_hr_manager')
    open_url_expires_at = fields.Datetime(string='Open URL Expires', copy=False)

    _sql_constraints = [
        ('signed_profile_uniq', 'unique(documenso_id, envelope_item_id)',
         'A signed profile already exists for this Documenso document / envelope item.'),
    ]

    @api.constrains('employee_id', 'applicant_id')
    def _check_recipient_reference(self):
        for record in self:
            if bool(record.employee_id) == bool(record.applicant_id):
                raise UserError(_(
                    "Signed profile must reference exactly one of Employee or Applicant."))

    @api.depends('employee_id.name', 'employee_id.work_email',
                 'applicant_id.partner_name', 'applicant_id.email_from')
    def _compute_recipient(self):
        for record in self:
            if record.employee_id:
                record.recipient_name = record.employee_id.name
                record.recipient_email = record.employee_id.work_email
            elif record.applicant_id:
                record.recipient_name = record.applicant_id.partner_name
                record.recipient_email = record.applicant_id.email_from
            else:
                record.recipient_name = False
                record.recipient_email = False

    @api.depends('employee_id.department_id', 'applicant_id.department_id')
    def _compute_department(self):
        for record in self:
            if record.employee_id and record.employee_id.department_id:
                record.department_id = record.employee_id.department_id
            elif record.applicant_id and record.applicant_id.department_id:
                record.department_id = record.applicant_id.department_id
            else:
                record.department_id = False

    @api.model
    def _upsert_from_contract(self, contract):
        if not contract or not contract.documenso_id:
            return self.browse()
        doc_class = contract.doc_class or (
            contract.template_id.doc_class if contract.template_id else 'contract')
        fields_json = self._fields_to_json(contract.field_ids)
        signed_at = contract.signed_at or fields.Datetime.now()

        results = self.browse()

        if contract.item_ids:
            for item in contract.item_ids:
                results |= self._upsert_single(
                    title=item.title or contract.name,
                    contract=contract,
                    doc_class=doc_class,
                    documenso_id=contract.documenso_id,
                    envelope_item_id=item.item_id or '',
                    pdf_binary=item.pdf_binary,
                    pdf_filename=item.pdf_filename,
                    external_url=item.redirect_url or False,
                    signed_at=signed_at,
                    fields_json=fields_json,
                )
        else:
            results |= self._upsert_single(
                title=contract.name,
                contract=contract,
                doc_class=doc_class,
                documenso_id=contract.documenso_id,
                envelope_item_id='',
                pdf_binary=contract.pdf_binary,
                pdf_filename=contract.pdf_filename,
                external_url=contract.signing_url or False,
                signed_at=signed_at,
                fields_json=fields_json,
            )
        return results

    @api.model
    def _upsert_single(self, title, contract, doc_class, documenso_id, envelope_item_id,
                       pdf_binary, pdf_filename, external_url, signed_at, fields_json):
        domain = [
            ('documenso_id', '=', documenso_id),
            ('envelope_item_id', '=', envelope_item_id or ''),
        ]
        record = self.search(domain, limit=1)
        vals = {
            'title': title,
            'employee_id': contract.employee_id.id if contract.employee_id else False,
            'applicant_id': contract.applicant_id.id if contract.applicant_id else False,
            'contract_id': contract.id,
            'template_id': contract.template_id.id if contract.template_id else False,
            'template_documenso_id': contract.template_id.documenso_id if contract.template_id else False,
            'doc_class': doc_class,
            'documenso_id': documenso_id,
            'envelope_id': contract.envelope_id or False,
            'envelope_item_id': envelope_item_id or '',
            'pdf_binary': pdf_binary or False,
            'pdf_filename': pdf_filename or False,
            'external_url': external_url or False,
            'signed_at': signed_at,
            'fields_json': fields_json,
            'raw_payload': contract.raw_payload,
        }
        if record:
            record.write(vals)
            return record
        return self.create(vals)

    @staticmethod
    def _fields_to_json(field_ids):
        if not field_ids:
            return False
        entries = [{
            'label': f.label or '',
            'type': f.field_type or '',
            'value': f.value or '',
            'recipient': f.recipient_email or '',
            'inserted': bool(f.inserted),
            'page': f.page or 1,
        } for f in field_ids]
        try:
            return json.dumps(entries, default=str, indent=2)
        except (TypeError, ValueError):
            return False

    def action_download_pdf(self):
        self.ensure_one()
        if self.pdf_binary:
            return {
                'type': 'ir.actions.act_url',
                'url': '/web/content/documenso.signed.profile/%s/pdf_binary/%s?download=true' % (
                    self.id, self.pdf_filename or ('signed-%s.pdf' % self.id)),
                'target': 'self',
            }
        if self.external_url:
            return {
                'type': 'ir.actions.act_url',
                'url': self.external_url,
                'target': 'new',
            }
        raise UserError(_("No downloadable content on this profile."))

    def action_generate_open_url(self):
        self.ensure_one()
        ttl = self._get_open_url_ttl()
        token = secrets.token_urlsafe(24)
        expires_at = fields.Datetime.now() + fields.timedelta(seconds=ttl)
        self.sudo().write({
            'open_url_token': token,
            'open_url_expires_at': expires_at,
        })
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        url = '%s/documenso/signed-profile/%s/open?token=%s' % (base.rstrip('/'), self.id, token)
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    @api.model
    def _get_open_url_ttl(self):
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(
                'documenso_connector.open_url_ttl_seconds') or DEFAULT_OPEN_URL_TTL_SECONDS)
        except (TypeError, ValueError):
            return DEFAULT_OPEN_URL_TTL_SECONDS

    def _verify_open_url_token(self, token):
        self.ensure_one()
        if not token or not self.open_url_token:
            return False
        if not self.open_url_expires_at:
            return False
        if self.open_url_expires_at < fields.Datetime.now():
            return False
        return hmac.compare_digest(str(self.open_url_token), str(token))

    def action_export_csv(self):
        return {
            'type': 'ir.actions.act_url',
            'url': '/documenso/signed-profile/export.csv',
            'target': 'self',
        }
