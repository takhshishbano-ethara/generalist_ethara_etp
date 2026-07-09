# -*- coding: utf-8 -*-
import hashlib
import hmac
import logging
import time
from urllib.parse import urljoin

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
DEFAULT_PAGE_SIZE = 25
DEFAULT_RATE_LIMIT_MS = 250
MAX_ATTEMPTS = 5
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

PREFILLABLE_TYPES = {
    'TEXT', 'NUMBER', 'DATE', 'CHECKBOX', 'DROPDOWN', 'RADIO', 'EMAIL', 'NAME',
}

FIELD_TYPE_MAP = {
    'TEXT': 'text',
    'NUMBER': 'number',
    'DATE': 'date',
    'CHECKBOX': 'checkbox',
    'DROPDOWN': 'dropdown',
    'RADIO': 'radio',
    'EMAIL': 'text',
    'INITIALS': 'text',
    'NAME': 'text',
    'FREE_SIGNATURE': 'text',
    'SIGNATURE': 'signature',
}


class DocumensoClient:

    def __init__(self, base_url, api_token, timeout=DEFAULT_TIMEOUT,
                 rate_limit_ms=DEFAULT_RATE_LIMIT_MS, signing_base_url=None,
                 webhook_secret=None):
        if not base_url or not api_token:
            raise UserError(_("Documenso API URL and API Token must be configured."))
        self.base_url = base_url.rstrip('/')
        self.api_token = api_token
        self.timeout = timeout
        self.rate_limit_seconds = max(0, (rate_limit_ms or 0) / 1000.0)
        self.signing_base_url = (signing_base_url or '').rstrip('/')
        self.webhook_secret = webhook_secret or ''
        self._last_call_ts = 0.0

    def _url(self, path):
        return urljoin(self.base_url + '/', path.lstrip('/'))

    def _headers(self, content_type='application/json'):
        headers = {
            'Authorization': self.api_token,
            'Accept': 'application/json',
        }
        if content_type:
            headers['Content-Type'] = content_type
        return headers

    def _throttle(self):
        if self.rate_limit_seconds <= 0:
            return
        elapsed = time.time() - self._last_call_ts
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)

    def _request(self, method, path, retry_download=False, **kwargs):
        url = self._url(path)
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(kwargs.pop('_content_type', 'application/json')
                                          if attempt == 1 else 'application/json'),
                    timeout=kwargs.pop('_timeout', self.timeout) if attempt == 1 else self.timeout,
                    **kwargs,
                )
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                _logger.warning("Documenso %s %s attempt %s failed: %s",
                                method, url, attempt, exc)
                self._last_call_ts = time.time()
                if attempt == MAX_ATTEMPTS:
                    raise UserError(_("Documenso request failed: %s") % exc) from exc
                time.sleep(min(60, 4 * (2 ** (attempt - 1))))
                continue

            self._last_call_ts = time.time()

            if response.status_code in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                _logger.warning("Documenso %s %s -> %s (attempt %s), retrying",
                                method, url, response.status_code, attempt)
                time.sleep(min(60, 4 * (2 ** (attempt - 1))))
                continue

            if response.status_code >= 400:
                _logger.error("Documenso %s %s -> %s: %s",
                              method, url, response.status_code, response.text[:500])
                raise UserError(_(
                    "Documenso API error (%(status)s): %(body)s"
                ) % {'status': response.status_code, 'body': response.text[:500]})

            if retry_download:
                return response
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {'raw': response.text}
        raise UserError(_("Documenso request exhausted retries: %s") % last_exc)

    def ping(self):
        return self._request('GET', '/template', params={'perPage': 1})

    def list_templates(self, page=1, per_page=DEFAULT_PAGE_SIZE):
        return self._request('GET', '/template',
                             params={'page': page, 'perPage': per_page})

    def iter_templates(self, per_page=DEFAULT_PAGE_SIZE):
        page = 1
        while True:
            payload = self.list_templates(page=page, per_page=per_page)
            items = self._extract_items(payload, ('templates', 'data', 'results'))
            if not items:
                break
            for item in items:
                yield item
            total_pages = self._extract_total_pages(payload, per_page)
            if page >= total_pages:
                break
            page += 1

    def get_template(self, template_id):
        return self._request('GET', '/template', params={'templateId': template_id})

    def use_template(self, template_id, recipients, distribute_document=True,
                     prefill_fields=None, title_override=None):
        body = {
            'templateId': template_id,
            'recipients': recipients or [],
            'distributeDocument': bool(distribute_document),
        }
        if prefill_fields:
            body['prefillFields'] = prefill_fields
        if title_override:
            body['override'] = {'title': title_override}
        return self._request('POST', '/template/use', json=body)

    def list_documents(self, page=1, per_page=DEFAULT_PAGE_SIZE, status=None,
                       order_by='createdAt', order_dir='desc'):
        params = {
            'page': page,
            'perPage': per_page,
            'orderByColumn': order_by,
            'orderByDirection': order_dir,
        }
        if status:
            params['status'] = status
        return self._request('GET', '/document', params=params)

    def iter_documents(self, per_page=DEFAULT_PAGE_SIZE, status=None):
        page = 1
        while True:
            payload = self.list_documents(page=page, per_page=per_page, status=status)
            items = self._extract_items(payload, ('documents', 'data', 'results'))
            if not items:
                break
            for item in items:
                yield item
            total_pages = self._extract_total_pages(payload, per_page)
            if page >= total_pages:
                break
            page += 1

    def get_document(self, document_id):
        payload = self._request('GET', '/document', params={'documentId': document_id})
        items = self._extract_items(payload, ('documents', 'data', 'results'))
        if items:
            return items[0]
        return payload

    def get_document_with_fields(self, document_id):
        return self._request('GET', '/document/%s' % document_id)

    def download_document_pdf(self, document_id):
        response = self._request(
            'GET',
            '/document/%s/download' % document_id,
            retry_download=True,
            allow_redirects=True,
            _timeout=DOWNLOAD_TIMEOUT,
        )
        content_type = response.headers.get('Content-Type', '')
        if content_type.startswith('application/json'):
            try:
                data = response.json()
            except ValueError:
                data = {}
            url = data.get('downloadUrl') or data.get('url')
            if url:
                return {'redirect_url': url}
        return {
            'content': response.content,
            'content_type': content_type or 'application/pdf',
        }

    def delete_document(self, document_id, envelope_id=None):
        if envelope_id:
            try:
                return self._request('POST', '/envelope/delete',
                                     json={'envelopeId': envelope_id})
            except UserError:
                _logger.info("Fallback to DELETE /document/%s", document_id)
        return self._request('DELETE', '/document/%s' % document_id)

    def get_envelope(self, envelope_id):
        return self._request('GET', '/envelope/%s' % envelope_id)

    def download_envelope_item_pdf(self, item_id):
        response = self._request(
            'GET',
            '/envelope/item/%s/download' % item_id,
            retry_download=True,
            allow_redirects=True,
            _timeout=DOWNLOAD_TIMEOUT,
        )
        content_type = response.headers.get('Content-Type', '')
        if content_type.startswith('application/json'):
            try:
                data = response.json()
            except ValueError:
                data = {}
            url = data.get('downloadUrl') or data.get('url')
            if url:
                return {'redirect_url': url}
        return {
            'content': response.content,
            'content_type': content_type or 'application/pdf',
        }

    def build_signing_url(self, token):
        if not token or not self.signing_base_url:
            return ''
        return '%s/%s' % (self.signing_base_url, token)

    @staticmethod
    def extract_signing_token(recipient_payload):
        if not isinstance(recipient_payload, dict):
            return ''
        return recipient_payload.get('token') or recipient_payload.get('signingToken') or ''

    @staticmethod
    def extract_document_id(payload):
        if not isinstance(payload, dict):
            return None
        for key in ('documentId', 'id', 'document_id'):
            value = payload.get(key)
            if value:
                return value
        for key in ('document', 'data'):
            nested = payload.get(key)
            if isinstance(nested, dict):
                found = DocumensoClient.extract_document_id(nested)
                if found:
                    return found
        return None

    def verify_webhook(self, raw_body, headers):
        if not self.webhook_secret:
            return True
        shared = headers.get('X-Documenso-Secret') or headers.get('x-documenso-secret')
        if shared and hmac.compare_digest(str(shared), str(self.webhook_secret)):
            return True
        signature = (headers.get('X-Documenso-Signature')
                     or headers.get('x-documenso-signature') or '')
        if not signature:
            return False
        return self._verify_hmac(raw_body, signature)

    def _verify_hmac(self, raw_body, signature_header):
        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')
        expected = hmac.new(
            self.webhook_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        for part in signature_header.split(','):
            part = part.strip()
            if part.startswith('v1='):
                candidate = part[3:]
                if hmac.compare_digest(candidate, expected):
                    return True
        return hmac.compare_digest(signature_header.strip(), expected)

    @staticmethod
    def _extract_items(payload, keys):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _extract_total_pages(payload, per_page):
        if isinstance(payload, dict):
            total_pages = payload.get('totalPages')
            if isinstance(total_pages, int) and total_pages > 0:
                return total_pages
            total = payload.get('total') or payload.get('count')
            if isinstance(total, int) and total > 0 and per_page > 0:
                return max(1, (total + per_page - 1) // per_page)
        return 1


def map_employee_fields(employee):
    if not employee:
        return {}
    partner = employee.address_home_id if hasattr(employee, 'address_home_id') else None
    def _val(rec, name, default=''):
        return getattr(rec, name, None) or default if rec else default

    address_parts = []
    if partner:
        for attr in ('street', 'street2', 'city', 'state_id', 'zip', 'country_id'):
            value = getattr(partner, attr, None)
            if hasattr(value, 'name'):
                value = value.name
            if value:
                address_parts.append(str(value))

    department = employee.department_id.name if employee.department_id else ''
    position = employee.job_id.name if employee.job_id else ''
    manager = employee.parent_id.name if employee.parent_id else ''

    joining_date = ''
    for attr in ('joining_date', 'first_contract_date', 'create_date'):
        raw = getattr(employee, attr, None)
        if raw:
            joining_date = str(raw)[:10]
            break

    mapping = {
        'name': employee.name or '',
        'full name': employee.name or '',
        'employee name': employee.name or '',
        'email': employee.work_email or _val(partner, 'email'),
        'work email': employee.work_email or '',
        'phone': employee.work_phone or _val(partner, 'phone'),
        'mobile': employee.mobile_phone or _val(partner, 'mobile'),
        'department': department,
        'position': position,
        'job title': position,
        'joining date': joining_date,
        'date of joining': joining_date,
        'employee id': employee.identification_id or '',
        'manager': manager,
        'address': ', '.join(address_parts),
        'city': _val(partner, 'city'),
        'country': partner.country_id.name if partner and partner.country_id else '',
    }
    for extra in ('pan', 'aadhaar', 'uan', 'bank_account_number', 'ifsc', 'ctc'):
        value = getattr(employee, extra, None)
        if value:
            mapping[extra.replace('_', ' ')] = str(value)
    return {k: v for k, v in mapping.items() if v}


def build_prefill_fields(template_fields, values):
    prefill = []
    for field in template_fields or []:
        if not isinstance(field, dict):
            continue
        raw_type = str(field.get('type') or field.get('fieldType') or '').upper()
        if raw_type not in PREFILLABLE_TYPES:
            continue
        label = str(field.get('label') or field.get('name') or '').strip().lower()
        if not label:
            continue
        value = values.get(label)
        if value is None:
            continue
        prefill.append({
            'id': field.get('id'),
            'type': FIELD_TYPE_MAP.get(raw_type, 'text'),
            'label': field.get('label') or field.get('name'),
            'value': str(value),
        })
    return prefill
