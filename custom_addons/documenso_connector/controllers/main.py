import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


def _iso(dt):
    return dt.isoformat() if dt else None


def _paginate(records, kwargs):
    try:
        page = max(1, int(kwargs.get('page') or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(1, int(kwargs.get('per_page') or 20)))
    except (TypeError, ValueError):
        per_page = 20
    total = len(records)
    offset = (page - 1) * per_page
    return records[offset:offset + per_page], {
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': (total + per_page - 1) // per_page if per_page else 1,
    }


def _serialize_template(t):
    return {
        'id': t.id,
        'documenso_id': t.documenso_id,
        'title': t.title,
        'doc_class': t.doc_class,
        'is_published': t.is_published,
        'job_id': t.job_id.id or None,
        'job_name': t.job_id.name or None,
        'salary_bracket': t.salary_bracket or None,
        'recipient_count': t.recipient_count,
        'field_count': t.field_count,
        'created_at': _iso(t.documenso_created_at),
        'updated_at': _iso(t.documenso_updated_at),
        'last_synced_at': _iso(t.last_synced_at),
    }


def _serialize_contract(c, include_pdf_url=False):
    data = {
        'id': c.id,
        'reference': c.name,
        'documenso_id': c.documenso_id,
        'status': c.status,
        'doc_class': c.doc_class,
        'applicant_id': c.applicant_id.id or None,
        'applicant_name': c.applicant_id.partner_name or None,
        'applicant_email': c.applicant_email or None,
        'job_id': c.job_id.id or None,
        'job_name': c.job_id.name or None,
        'template_id': c.template_id.id or None,
        'template_title': c.template_id.title or None,
        'template_ids': c.template_ids.ids,
        'signing_url': c.signing_url or None,
        'sent_at': _iso(c.sent_at),
        'signed_at': _iso(c.signed_at),
        'has_signed_pdf': bool(c.pdf_binary),
        'pdf_filename': c.pdf_filename or None,
    }
    if include_pdf_url and c.pdf_binary:
        data['pdf_url'] = '/web/content/documenso.contract/%s/pdf_binary/%s?download=true' % (
            c.id, c.pdf_filename or 'signed.pdf',
        )
    return data


class DocumensoWebhook(http.Controller):

    @http.route('/documenso/webhook', type='http', auth='none', csrf=False,
                methods=['POST'])
    def documenso_webhook(self, **_kwargs):
        raw = request.httprequest.get_data() or b''
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _logger.warning("Documenso webhook: invalid JSON: %s", exc)
            return request.make_response(
                json.dumps({'error': 'invalid json'}),
                headers=[('Content-Type', 'application/json')], status=400,
            )
        Settings = request.env['res.config.settings'].sudo()
        client = Settings.get_client()
        headers = dict(request.httprequest.headers)
        if not client.verify_webhook(raw, headers):
            _logger.warning("Documenso webhook signature invalid")
            return request.make_response(
                json.dumps({'error': 'invalid signature'}),
                headers=[('Content-Type', 'application/json')], status=401,
            )
        event = payload.get('event') or payload.get('type') or ''
        document_payload = payload
        for key in ('payload', 'data', 'document'):
            nested = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(nested, dict):
                document_payload = nested
                break
        request.env['documenso.contract'].sudo()._process_webhook(event, document_payload)
        return request.make_response(
            json.dumps({'status': 'ok'}),
            headers=[('Content-Type', 'application/json')], status=200,
        )


class DocumensoRestApi(http.Controller):

    @http.route('/api/v1/documenso/templates', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_templates(self, **kwargs):
        env = request.env['documenso.template'].sudo()
        domain = [('active', '=', True)]
        job_id = kwargs.get('job_id')
        if job_id:
            try:
                domain.append(('job_id', '=', int(job_id)))
            except (TypeError, ValueError):
                pass
        doc_class = kwargs.get('doc_class')
        if doc_class in ('contract', 'compliance'):
            domain.append(('doc_class', '=', doc_class))
        if kwargs.get('published') in ('1', 'true', 'True'):
            domain.append(('is_published', '=', True))
        records = env.search(domain, order='title asc')
        page_records, pagination = _paginate(records, kwargs)
        return return_Response(message='Success', status=200, data={
            'records': [_serialize_template(t) for t in page_records],
            'pagination': pagination,
        })

    @http.route('/api/v1/documenso/documents', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_documents(self, **kwargs):
        env = request.env['documenso.contract'].sudo()
        domain = []
        for key, field in (('status', 'status'), ('doc_class', 'doc_class')):
            val = kwargs.get(key)
            if val:
                domain.append((field, '=', val))
        for key, field in (('applicant_id', 'applicant_id'),
                           ('job_id', 'job_id'), ('template_id', 'template_id')):
            val = kwargs.get(key)
            if val:
                try:
                    domain.append((field, '=', int(val)))
                except (TypeError, ValueError):
                    pass
        records = env.search(domain, order='sent_at desc, id desc')
        page_records, pagination = _paginate(records, kwargs)
        return return_Response(message='Success', status=200, data={
            'records': [_serialize_contract(c, include_pdf_url=True) for c in page_records],
            'pagination': pagination,
        })

    @http.route('/api/v1/documenso/documents/<int:doc_id>', methods=['GET'],
                type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def get_document(self, doc_id, **_kwargs):
        record = request.env['documenso.contract'].sudo().browse(doc_id)
        if not record.exists():
            return return_Response(message='Document not found.', status=404)
        return return_Response(message='Success', status=200, data={
            'record': _serialize_contract(record, include_pdf_url=True),
        })

    @http.route('/api/v1/documenso/signed-documents', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def list_signed(self, **kwargs):
        env = request.env['documenso.contract'].sudo()
        domain = [('status', '=', 'SIGNED')]
        for key, field in (('applicant_id', 'applicant_id'),
                           ('job_id', 'job_id'), ('template_id', 'template_id')):
            val = kwargs.get(key)
            if val:
                try:
                    domain.append((field, '=', int(val)))
                except (TypeError, ValueError):
                    pass
        doc_class = kwargs.get('doc_class')
        if doc_class in ('contract', 'compliance'):
            domain.append(('doc_class', '=', doc_class))
        records = env.search(domain, order='signed_at desc, id desc')
        page_records, pagination = _paginate(records, kwargs)
        return return_Response(message='Success', status=200, data={
            'records': [_serialize_contract(c, include_pdf_url=True) for c in page_records],
            'pagination': pagination,
        })
