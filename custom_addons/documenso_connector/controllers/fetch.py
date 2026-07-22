import base64
import logging

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)


def _get_client():
    return request.env['res.config.settings'].sudo().get_client()


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_items(payload):
    if not isinstance(payload, dict):
        return []
    for key in ('data', 'templates', 'documents', 'results'):
        items = payload.get(key)
        if isinstance(items, list):
            return items
    if payload.get('id') or payload.get('templateId') or payload.get('documentId'):
        return [payload]
    return []


def _upsert_templates(items):
    Template = request.env['documenso.template'].sudo()
    created_ids, updated_ids = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        docid = str(item.get('id') or item.get('templateId') or '')
        if not docid:
            continue
        existed = bool(Template.search([('documenso_id', '=', docid)], limit=1))
        record, _was_created = Template._upsert_from_list_payload(item)
        if not record:
            continue
        (updated_ids if existed else created_ids).append(record.id)
    return {'created_ids': created_ids, 'updated_ids': updated_ids}


def _upsert_contracts(items):
    Contract = request.env['documenso.contract'].sudo()
    updated_ids, skipped = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        docid = str(item.get('id') or item.get('documentId') or '')
        if not docid:
            continue
        record = Contract.search([('documenso_id', '=', docid)], limit=1)
        if not record:
            skipped.append(docid)
            continue
        record._apply_document_payload(item)
        updated_ids.append(record.id)
    return {'updated_ids': updated_ids, 'skipped_documenso_ids': skipped}


class DocumensoFetch(http.Controller):

    @http.route('/api/v1/documenso/fetch/templates', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_templates(self, **kwargs):
        client = _get_client()
        page = _int(kwargs.get('page'), 1)
        per_page = min(100, max(1, _int(kwargs.get('per_page'), 25)))
        try:
            payload = client.list_templates(page=page, per_page=per_page)
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        upsert = _upsert_templates(_extract_items(payload))
        return return_Response(message='Success', status=200, data={
            'source': 'documenso',
            'page': page, 'per_page': per_page,
            'payload': payload,
            'odoo': upsert,
        })

    @http.route('/api/v1/documenso/fetch/template/<int:template_id>',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_template(self, template_id, **_kwargs):
        client = _get_client()
        try:
            payload = client.get_template(template_id)
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        upsert = _upsert_templates(_extract_items(payload))
        return return_Response(message='Success', status=200, data={
            'source': 'documenso', 'payload': payload, 'odoo': upsert,
        })

    @http.route('/api/v1/documenso/fetch/documents', methods=['GET'], type='http',
                auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_documents(self, **kwargs):
        client = _get_client()
        page = _int(kwargs.get('page'), 1)
        per_page = min(100, max(1, _int(kwargs.get('per_page'), 25)))
        status = kwargs.get('status') or None
        try:
            payload = client.list_documents(page=page, per_page=per_page, status=status)
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        upsert = _upsert_contracts(_extract_items(payload))
        return return_Response(message='Success', status=200, data={
            'source': 'documenso',
            'page': page, 'per_page': per_page, 'status': status,
            'payload': payload,
            'odoo': upsert,
        })

    @http.route('/api/v1/documenso/fetch/document/<int:document_id>',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_document(self, document_id, **kwargs):
        client = _get_client()
        include_fields = kwargs.get('include_fields') in ('1', 'true', 'True')
        try:
            payload = (client.get_document_with_fields(document_id)
                       if include_fields else client.get_document(document_id))
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        upsert = _upsert_contracts(_extract_items(payload))
        return return_Response(message='Success', status=200, data={
            'source': 'documenso', 'payload': payload, 'odoo': upsert,
        })

    @http.route('/api/v1/documenso/fetch/signed-documents',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_signed_documents(self, **kwargs):
        client = _get_client()
        page = _int(kwargs.get('page'), 1)
        per_page = min(100, max(1, _int(kwargs.get('per_page'), 25)))
        try:
            payload = client.list_documents(page=page, per_page=per_page, status='COMPLETED')
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        upsert = _upsert_contracts(_extract_items(payload))
        return return_Response(message='Success', status=200, data={
            'source': 'documenso',
            'page': page, 'per_page': per_page,
            'payload': payload,
            'odoo': upsert,
        })

    @http.route('/api/v1/documenso/fetch/document/<int:document_id>/pdf',
                methods=['GET'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def fetch_document_pdf(self, document_id, **_kwargs):
        client = _get_client()
        try:
            result = client.download_document_pdf(document_id)
        except UserError as exc:
            return return_Response(message=str(exc), status=502)
        if isinstance(result, dict) and result.get('content'):
            return return_Response(message='Success', status=200, data={
                'source': 'documenso',
                'content_type': result.get('content_type') or 'application/pdf',
                'content_base64': base64.b64encode(result['content']).decode('ascii'),
            })
        if isinstance(result, dict) and result.get('redirect_url'):
            return return_Response(message='Success', status=200, data={
                'source': 'documenso',
                'redirect_url': result['redirect_url'],
            })
        return return_Response(message='No PDF returned by Documenso.', status=404)
