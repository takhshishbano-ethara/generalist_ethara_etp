import base64
import csv
import io
import json
import logging

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    safe_get_value,
    validate_token,
)

_logger = logging.getLogger(__name__)

CONTRACT_DOC_CLASSES = {'contract', 'compliance'}
CONTRACT_STATUS_CODES = {'DRAFT', 'SENT', 'OPENED', 'SIGNED', 'REJECTED', 'CANCELLED', 'EXPIRED'}
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class DocumensoController(http.Controller):

    @http.route('/documenso/download/<int:doc_id>', type='http', auth='user')
    def download_document(self, doc_id, **kwargs):
        document = request.env['documenso.document'].browse(doc_id)
        try:
            document.check_access_rights('read')
            document.check_access_rule('read')
        except AccessError:
            return request.not_found()
        if not document.exists():
            return request.not_found()

        document.sudo()._cache_download()
        if not document.download_binary:
            return request.not_found()

        content = base64.b64decode(document.download_binary)
        filename = document.download_filename or f'documenso-{document.documenso_id or doc_id}.pdf'
        headers = [
            ('Content-Type', 'application/pdf'),
            ('Content-Length', str(len(content))),
            ('Content-Disposition', http.content_disposition(filename)),
        ]
        return request.make_response(content, headers=headers)

    @http.route('/documenso/view/<int:doc_id>', type='http', auth='user')
    def view_document(self, doc_id, **kwargs):
        document = request.env['documenso.document'].browse(doc_id)
        try:
            document.check_access_rights('read')
            document.check_access_rule('read')
        except AccessError:
            return request.not_found()
        if not document.exists() or not document.view_url:
            return request.not_found()
        return request.redirect(document.view_url, local=False)

    @http.route('/documenso/webhook', type='http', auth='none', csrf=False, methods=['POST'])
    def webhook(self, **kwargs):
        raw = request.httprequest.get_data() or b''
        headers = dict(request.httprequest.headers)
        client = request.env['res.config.settings'].sudo().get_client()
        try:
            if not client.verify_webhook(raw, headers):
                _logger.warning("Documenso webhook signature verification failed")
                return request.make_response(json.dumps({'status': 'unauthorized'}), status=401,
                                             headers=[('Content-Type', 'application/json')])
        except Exception:
            _logger.exception("Documenso webhook verification error")
            return request.make_response(json.dumps({'status': 'error'}), status=500,
                                         headers=[('Content-Type', 'application/json')])

        try:
            payload = json.loads(raw.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return request.make_response(json.dumps({'status': 'bad_request'}), status=400,
                                         headers=[('Content-Type', 'application/json')])

        event = payload.get('event') or payload.get('type') or ''
        document_payload = (
            payload.get('payload')
            or payload.get('document')
            or payload.get('data')
            or payload
        )
        try:
            request.env['documenso.contract'].sudo()._process_webhook(event, document_payload)
        except Exception:
            _logger.exception("Documenso webhook processing failed for event=%s", event)
            return request.make_response(json.dumps({'status': 'error'}), status=500,
                                         headers=[('Content-Type', 'application/json')])

        return request.make_response(json.dumps({'status': 'ok'}), status=200,
                                     headers=[('Content-Type', 'application/json')])

    @http.route('/documenso/signed-profile/<int:profile_id>/open', type='http', auth='public')
    def signed_profile_open(self, profile_id, token=None, **kwargs):
        profile = request.env['documenso.signed.profile'].sudo().browse(profile_id)
        if not profile.exists() or not profile._verify_open_url_token(token or ''):
            return request.not_found()
        if profile.pdf_binary:
            content = base64.b64decode(profile.pdf_binary)
            filename = profile.pdf_filename or ('signed-%s.pdf' % profile_id)
            return request.make_response(content, headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Length', str(len(content))),
                ('Content-Disposition', http.content_disposition(filename)),
            ])
        if profile.external_url:
            return request.redirect(profile.external_url, local=False)
        return request.not_found()

    @http.route('/documenso/signed-profile/export.csv', type='http', auth='user')
    def signed_profile_export(self, doc_class=None, employee_id=None, template_id=None, **kwargs):
        Profile = request.env['documenso.signed.profile']
        try:
            Profile.check_access_rights('read')
        except AccessError:
            return request.not_found()
        domain = []
        if doc_class:
            domain.append(('doc_class', '=', doc_class))
        if employee_id:
            try:
                domain.append(('employee_id', '=', int(employee_id)))
            except (TypeError, ValueError):
                pass
        if template_id:
            try:
                domain.append(('template_id', '=', int(template_id)))
            except (TypeError, ValueError):
                pass
        records = Profile.search(domain, order='signed_at desc')

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'signed_at', 'title', 'doc_class', 'employee', 'employee_email',
            'department', 'template', 'documenso_id', 'envelope_id', 'envelope_item_id',
        ])
        for rec in records:
            writer.writerow([
                rec.signed_at.isoformat() if rec.signed_at else '',
                rec.title or '',
                rec.doc_class or '',
                rec.employee_id.name or '',
                rec.employee_email or '',
                rec.department_id.name or '',
                rec.template_id.title or '',
                rec.documenso_id or '',
                rec.envelope_id or '',
                rec.envelope_item_id or '',
            ])
        payload = buffer.getvalue().encode('utf-8')
        return request.make_response(payload, headers=[
            ('Content-Type', 'text/csv; charset=utf-8'),
            ('Content-Length', str(len(payload))),
            ('Content-Disposition', http.content_disposition('signed-documents.csv')),
        ])

    def _base_url(self):
        base = request.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        return base.rstrip('/')

    def _abs_url(self, path):
        if not path:
            return ''
        base = self._base_url()
        return '%s%s' % (base, path) if base else path

    def _serialize_contract(self, contract, include_items=False, include_fields=False):
        base = self._base_url()
        pdf_url = ''
        if contract.pdf_binary:
            filename = contract.pdf_filename or ('contract-%s.pdf' % contract.id)
            pdf_url = '%s/web/content/documenso.contract/%s/pdf_binary/%s?download=true' % (
                base, contract.id, filename)

        templates = [{
            'id': safe_get_value(tpl, 'id', 'int'),
            'documenso_id': safe_get_value(tpl, 'documenso_id', 'str'),
            'title': safe_get_value(tpl, 'title', 'str'),
            'doc_class': safe_get_value(tpl, 'doc_class', 'str'),
        } for tpl in contract.template_ids]

        data = {
            'id': safe_get_value(contract, 'id', 'int'),
            'name': safe_get_value(contract, 'name', 'str'),
            'doc_class': safe_get_value(contract, 'doc_class', 'str'),
            'status': safe_get_value(contract, 'status', 'str'),
            'documenso_id': safe_get_value(contract, 'documenso_id', 'str'),
            'envelope_id': safe_get_value(contract, 'envelope_id', 'str'),
            'signing_url': safe_get_value(contract, 'signing_url', 'str'),
            'pdf_filename': safe_get_value(contract, 'pdf_filename', 'str'),
            'pdf_download_url': pdf_url,
            'sent_at': safe_get_value(contract, 'sent_at', 'datetime'),
            'signed_at': safe_get_value(contract, 'signed_at', 'datetime'),
            'last_synced_at': safe_get_value(contract, 'last_synced_at', 'datetime'),
            'employee': {
                'id': safe_get_value(contract, 'employee_id.id', 'int'),
                'name': safe_get_value(contract, 'employee_id.name', 'str'),
                'email': safe_get_value(contract, 'employee_id.work_email', 'str'),
                'department_id': safe_get_value(contract, 'employee_id.department_id.id', 'int'),
                'department_name': safe_get_value(contract, 'employee_id.department_id.name', 'str'),
            },
            'template': {
                'id': safe_get_value(contract, 'template_id.id', 'int'),
                'documenso_id': safe_get_value(contract, 'template_id.documenso_id', 'str'),
                'title': safe_get_value(contract, 'template_id.title', 'str'),
            },
            'templates': templates,
            'item_count': safe_get_value(contract, 'item_count', 'int'),
            'field_count': safe_get_value(contract, 'field_count', 'int'),
        }

        if include_items:
            data['items'] = [{
                'id': safe_get_value(item, 'id', 'int'),
                'item_id': safe_get_value(item, 'item_id', 'str'),
                'title': safe_get_value(item, 'title', 'str'),
                'category': safe_get_value(item, 'category', 'str'),
                'sequence': safe_get_value(item, 'sequence', 'int'),
                'pdf_filename': safe_get_value(item, 'pdf_filename', 'str'),
                'pdf_download_url': ('%s/web/content/documenso.contract.item/%s/pdf_binary/%s?download=true' % (
                    base, item.id, item.pdf_filename or ('item-%s.pdf' % item.id))) if item.pdf_binary else '',
                'redirect_url': safe_get_value(item, 'redirect_url', 'str'),
            } for item in contract.item_ids]

        if include_fields:
            data['fields'] = [{
                'id': safe_get_value(f, 'id', 'int'),
                'documenso_id': safe_get_value(f, 'documenso_id', 'str'),
                'label': safe_get_value(f, 'label', 'str'),
                'type': safe_get_value(f, 'field_type', 'str'),
                'value': safe_get_value(f, 'value', 'str'),
                'inserted': safe_get_value(f, 'inserted', 'bool'),
                'recipient_email': safe_get_value(f, 'recipient_email', 'str'),
                'page': safe_get_value(f, 'page', 'int'),
            } for f in contract.field_ids]

        return data

    def _build_contract_domain(self, params):
        domain = []

        doc_class = (params.get('doc_class') or '').strip().lower()
        if doc_class:
            if doc_class not in CONTRACT_DOC_CLASSES:
                return None, "Invalid doc_class. Allowed values: %s." % ', '.join(sorted(CONTRACT_DOC_CLASSES))
            domain.append(('doc_class', '=', doc_class))

        status = (params.get('status') or '').strip().upper()
        if status:
            if status not in CONTRACT_STATUS_CODES:
                return None, "Invalid status. Allowed values: %s." % ', '.join(sorted(CONTRACT_STATUS_CODES))
            domain.append(('status', '=', status))

        for key, field in (('employee_id', 'employee_id'), ('template_id', 'template_id')):
            raw = params.get(key)
            if raw in (None, ''):
                continue
            try:
                domain.append((field, '=', int(raw)))
            except (TypeError, ValueError):
                return None, "Invalid %s. Must be an integer." % key

        documenso_id = (params.get('documenso_id') or '').strip()
        if documenso_id:
            domain.append(('documenso_id', '=', documenso_id))

        search = (params.get('search') or '').strip()
        if search:
            domain += ['|', '|',
                       ('name', 'ilike', search),
                       ('employee_id.name', 'ilike', search),
                       ('employee_id.work_email', 'ilike', search)]

        return domain, None

    @http.route('/api/v1/documenso/documents', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def api_list_documents(self, **params):
        dummy_records = [
            {
                'id': 1,
                'name': 'CT/2026/0001',
                'doc_class': 'contract',
                'status': 'SIGNED',
                'documenso_id': 'doc_abc123',
                'envelope_id': 'env_abc123',
                'signing_url': 'https://documenso.example.com/sign/doc_abc123',
                'employee': {
                    'id': 11,
                    'name': 'Aakash Vishwakarma',
                    'email': 'aakash@ethara.ai',
                    'department_id': 3,
                    'department_name': 'Engineering',
                },
                'template': {
                    'id': 5,
                    'documenso_id': 'tpl_offer_v2',
                    'title': 'Offer Letter v2',
                },
                'templates': [
                    {'id': 5, 'documenso_id': 'tpl_offer_v2', 'title': 'Offer Letter v2'},
                ],
                'sent_at': '2026-07-01 10:15:30',
                'signed_at': '2026-07-02 14:22:10',
                'last_synced_at': '2026-07-02 14:23:00',
                'item_count': 1,
                'field_count': 6,
                'pdf_filename': 'offer_letter_signed.pdf',
                'pdf_download_url': 'http://localhost:8069/web/content/documenso.contract/1/pdf_binary/offer_letter_signed.pdf?download=true',
                'note': '',
            },
            {
                'id': 2,
                'name': 'CM/2026/0002',
                'doc_class': 'compliance',
                'status': 'SENT',
                'documenso_id': 'doc_xyz789',
                'envelope_id': 'env_xyz789',
                'signing_url': 'https://documenso.example.com/sign/doc_xyz789',
                'employee': {
                    'id': 12,
                    'name': 'Priya Sharma',
                    'email': 'priya@ethara.ai',
                    'department_id': 4,
                    'department_name': 'HR',
                },
                'template': {
                    'id': 8,
                    'documenso_id': 'tpl_pos_v1',
                    'title': 'POSH Policy Acknowledgement',
                },
                'templates': [
                    {'id': 8, 'documenso_id': 'tpl_pos_v1', 'title': 'POSH Policy Acknowledgement'},
                ],
                'sent_at': '2026-07-05 09:00:00',
                'signed_at': '',
                'last_synced_at': '2026-07-05 09:00:05',
                'item_count': 1,
                'field_count': 3,
                'pdf_filename': '',
                'pdf_download_url': '',
                'note': 'Awaiting signature',
            },
        ]
        data = {
            'records': dummy_records,
            'pagination': {
                'total': len(dummy_records),
                'page': int(params.get('page') or 1),
                'limit': int(params.get('limit') or DEFAULT_PAGE_SIZE),
                'pages': 1,
            },
        }
        return return_Response(message="Success", status=200, data=data)
        # try:
        #     domain, error = self._build_contract_domain(params)
        #     if error:
        #         return return_Response(message=error, status=400)
        #
        #     try:
        #         page = max(int(params.get('page') or 1), 1)
        #     except (TypeError, ValueError):
        #         return return_Response(message="Invalid page. Must be an integer.", status=400)
        #     try:
        #         limit = int(params.get('limit') or DEFAULT_PAGE_SIZE)
        #     except (TypeError, ValueError):
        #         return return_Response(message="Invalid limit. Must be an integer.", status=400)
        #     limit = max(1, min(limit, MAX_PAGE_SIZE))
        #     offset = (page - 1) * limit
        #
        #     Contract = request.env['documenso.contract'].sudo()
        #     total = Contract.search_count(domain)
        #     records = Contract.search(domain, offset=offset, limit=limit, order='sent_at desc, id desc')
        #
        #     data = {
        #         'records': [self._serialize_contract(rec) for rec in records],
        #         'pagination': {
        #             'total': total,
        #             'page': page,
        #             'limit': limit,
        #             'pages': (total + limit - 1) // limit if total else 0,
        #         },
        #     }
        #     return return_Response(message="Success", status=200, data=data)
        # except Exception as e:
        #     _logger.exception("Failed to list Documenso documents")
        #     return return_Response(message="Something went wrong.", status=400, errors=[str(e)])

    @http.route('/api/v1/documenso/documents/<int:doc_id>', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def api_get_document(self, doc_id, **params):
        dummy_record = {
            'id': doc_id,
            'name': 'CT/2026/%04d' % doc_id,
            'doc_class': 'contract',
            'status': 'SIGNED',
            'documenso_id': 'doc_abc123',
            'envelope_id': 'env_abc123',
            'signing_url': 'https://documenso.example.com/sign/doc_abc123',
            'employee': {
                'id': 11,
                'name': 'Aakash Vishwakarma',
                'email': 'aakash@ethara.ai',
                'department_id': 3,
                'department_name': 'Engineering',
            },
            'template': {
                'id': 5,
                'documenso_id': 'tpl_offer_v2',
                'title': 'Offer Letter v2',
            },
            'templates': [
                {'id': 5, 'documenso_id': 'tpl_offer_v2', 'title': 'Offer Letter v2'},
                {'id': 6, 'documenso_id': 'tpl_nda_v1', 'title': 'Mutual NDA v1'},
            ],
            'sent_at': '2026-07-01 10:15:30',
            'signed_at': '2026-07-02 14:22:10',
            'last_synced_at': '2026-07-02 14:23:00',
            'item_count': 2,
            'field_count': 6,
            'pdf_filename': 'offer_letter_signed.pdf',
            'pdf_download_url': 'http://localhost:8069/web/content/documenso.contract/%d/pdf_binary/offer_letter_signed.pdf?download=true' % doc_id,
            'note': '',
            'items': [
                {
                    'id': 101,
                    'item_id': 'item_offer_letter',
                    'title': 'Offer Letter',
                    'category': 'offer_letter',
                    'sequence': 1,
                    'pdf_filename': 'offer_letter.pdf',
                    'pdf_download_url': 'http://localhost:8069/web/content/documenso.contract.item/101/pdf_binary/offer_letter.pdf?download=true',
                    'redirect_url': 'https://documenso.example.com/redirect/offer',
                },
                {
                    'id': 102,
                    'item_id': 'item_nda',
                    'title': 'Mutual NDA',
                    'category': 'nda',
                    'sequence': 2,
                    'pdf_filename': 'nda.pdf',
                    'pdf_download_url': 'http://localhost:8069/web/content/documenso.contract.item/102/pdf_binary/nda.pdf?download=true',
                    'redirect_url': 'https://documenso.example.com/redirect/nda',
                },
            ],
            'fields': [
                {
                    'id': 201,
                    'documenso_id': 'fld_name',
                    'label': 'Full Name',
                    'field_type': 'TEXT',
                    'value': 'Aakash Vishwakarma',
                    'inserted': True,
                    'recipient_email': 'aakash@ethara.ai',
                    'page': 1,
                },
                {
                    'id': 202,
                    'documenso_id': 'fld_ctc',
                    'label': 'Annual CTC (INR)',
                    'field_type': 'NUMBER',
                    'value': '2400000',
                    'inserted': True,
                    'recipient_email': 'aakash@ethara.ai',
                    'page': 1,
                },
                {
                    'id': 203,
                    'documenso_id': 'fld_join_date',
                    'label': 'Joining Date',
                    'field_type': 'DATE',
                    'value': '2026-07-15',
                    'inserted': True,
                    'recipient_email': 'aakash@ethara.ai',
                    'page': 1,
                },
                {
                    'id': 204,
                    'documenso_id': 'fld_sig',
                    'label': 'Signature',
                    'field_type': 'SIGNATURE',
                    'value': '',
                    'inserted': True,
                    'recipient_email': 'aakash@ethara.ai',
                    'page': 2,
                },
            ],
        }
        return return_Response(message="Success", status=200, data={'record': dummy_record})
        # try:
        #     contract = request.env['documenso.contract'].sudo().browse(doc_id)
        #     if not contract.exists():
        #         return return_Response(message="Document not found.", status=404)
        #     data = {'record': self._serialize_contract(contract, include_items=True, include_fields=True)}
        #     return return_Response(message="Success", status=200, data=data)
        # except Exception as e:
        #     _logger.exception("Failed to fetch Documenso document %s", doc_id)
        #     return return_Response(message="Something went wrong.", status=400, errors=[str(e)])
