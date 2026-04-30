# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import random
import requests
import os
import logging
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

# URL schemes and domain suffixes accepted by the JSONL import endpoint.
# Extend this tuple when new trusted sources are introduced.
_ALLOWED_URL_SCHEMES = ("https",)
_ALLOWED_URL_DOMAIN_SUFFIXES = (
    ".amazonaws.com",
    ".s3.amazonaws.com",
    ".storage.googleapis.com",
    ".blob.core.windows.net",
)

# Request body / response size caps for the JSONL import endpoint. Caps
# bound memory use on pathological input (unbounded body, multi-GB S3
# object). Hostile review: AA (F12) and BB (F13).
_JSONL_MAX_REQUEST_BYTES = 10 * 1024 * 1024          # 10 MB
_JSONL_MAX_RESPONSE_BYTES = 100 * 1024 * 1024        # 100 MB
_JSONL_RESPONSE_CHUNK = 1024 * 1024                  # 1 MB streaming chunk


def _is_url_allowed(url):
    """Return True only when *url* points to a trusted, non-internal host."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        return False

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False

    # Block obvious internal/link-local/metadata ranges
    _BLOCKED_HOSTS = (
        "localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254",
        "[::1]", "metadata.google.internal",
    )
    if hostname in _BLOCKED_HOSTS:
        return False
    if hostname.endswith(".internal") or hostname.endswith(".local"):
        return False

    if not any(hostname.endswith(suffix) for suffix in _ALLOWED_URL_DOMAIN_SUFFIXES):
        return False

    return True


class Atlas(http.Controller):
    @http.route('/api/get_jsonl_data', type='http', auth='user', methods=['POST'], csrf=True)
    def method_get_jsonl_data(self, **params):
        try:
            if 'atlas.persona' not in request.env or 'atlas.taxonomy' not in request.env:
                _logger.error(
                    "JSONL import unavailable: required models missing "
                    "(atlas.persona=%s, atlas.taxonomy=%s)",
                    'atlas.persona' in request.env,
                    'atlas.taxonomy' in request.env,
                )
                return http.Response(
                    json.dumps({
                        'message': 'JSONL import feature is not available in this deployment',
                        'status': 501,
                    }),
                    content_type='application/json',
                    status=501,
                )

            try:
                content_length = int(request.httprequest.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > _JSONL_MAX_REQUEST_BYTES:
                return http.Response(
                    json.dumps({
                        'message': 'Request body too large (max %d bytes)' % _JSONL_MAX_REQUEST_BYTES,
                        'status': 413,
                    }),
                    content_type='application/json',
                    status=413,
                )
            raw_body = request.httprequest.stream.read(_JSONL_MAX_REQUEST_BYTES + 1)
            if len(raw_body) > _JSONL_MAX_REQUEST_BYTES:
                return http.Response(
                    json.dumps({
                        'message': 'Request body exceeded size cap (max %d bytes)' % _JSONL_MAX_REQUEST_BYTES,
                        'status': 413,
                    }),
                    content_type='application/json',
                    status=413,
                )
            try:
                jdata = json.loads(raw_body) if raw_body else {}
            except Exception:
                try:
                    jdata = json.loads(request.httprequest.data)
                except Exception:
                    jdata = {}
            if 'url' not in jdata:
                return http.Response(
                    json.dumps({'message': 'URL not in body', 'status': 400}),
                    content_type='application/json',
                    status=400
                )
            if not jdata['url']:
                return http.Response(
                    json.dumps({'message': 'URL is empty', 'status': 400}),
                    content_type='application/json',
                    status=400
                )
            url = str(jdata['url'])

            if not _is_url_allowed(url):
                _logger.warning(
                    "JSONL import rejected URL (failed allowlist): %s", url[:200],
                )
                return http.Response(
                    json.dumps({
                        'message': 'URL not allowed. Only HTTPS URLs to approved cloud storage domains are accepted.',
                        'status': 403,
                    }),
                    content_type='application/json',
                    status=403
                )

            with requests.get(
                url, timeout=60, allow_redirects=False, stream=True,
            ) as response:
                response.raise_for_status()
                chunks = []
                total = 0
                for chunk in response.iter_content(
                    chunk_size=_JSONL_RESPONSE_CHUNK, decode_unicode=False,
                ):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _JSONL_MAX_RESPONSE_BYTES:
                        _logger.warning(
                            "JSONL import aborted: response exceeded %d bytes for %s",
                            _JSONL_MAX_RESPONSE_BYTES, url[:200],
                        )
                        return http.Response(
                            json.dumps({
                                'message': 'Remote file too large (max %d bytes)' % _JSONL_MAX_RESPONSE_BYTES,
                                'status': 413,
                            }),
                            content_type='application/json',
                            status=413,
                        )
                    chunks.append(chunk)
                response_text = b"".join(chunks).decode(
                    response.encoding or 'utf-8', errors='replace',
                )

            data = []
            for line in response_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                line = line.replace('"gog_auth"{', '"gog_auth":{')
                line = line.rstrip(',')
                diff = line.count('{') - line.count('}')
                if diff > 0:
                    line = line + '}' * diff
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError as parse_err:
                    _logger.warning("Skipping malformed JSONL line: %s", str(parse_err)[:200])
                    continue

            if not data:
                return http.Response(
                    json.dumps({'message': 'Data Not Found', 'status': 400}),
                    content_type='application/json',
                    status=400
                )

            AtlasModel = request.env['atlas.atlas'].sudo()
            PersonaModel = request.env['atlas.persona'].sudo()
            DomainModel = request.env['atlas.domain'].sudo()
            TaxonomyModel = request.env['atlas.taxonomy'].sudo()

            created_ids = []
            for item in data:
                task_id = item.get('id', '')

                # Skip duplicates
                if task_id and AtlasModel.search([('task_id', '=', task_id)], limit=1):
                    _logger.info("Skipping duplicate task_id: %s", task_id)
                    continue

                # Auto-create or find persona
                persona_name = (item.get('persona') or '').strip()
                if persona_name:
                    normalized_name = persona_name.lower().replace(' ', '-')
                    persona = PersonaModel.search([('name', '=', normalized_name)], limit=1)
                    if not persona:
                        persona = PersonaModel.create({
                            'name': persona_name,
                            'soul_md': item.get('soul.md', ''),
                            'memory_md': item.get('memory.md', ''),
                            'agents_md': item.get('agent.md', ''),
                        })
                else:
                    persona = PersonaModel.search([], limit=1)
                    if not persona:
                        persona = PersonaModel.create({'name': 'default'})

                # Build gog_auth as JSON string
                gog_auth_val = item.get('gog_auth')
                gog_auth_str = json.dumps(gog_auth_val) if gog_auth_val else ''

                creds = item.get('credentials', {})
                gmail_creds = creds.get('gmail', {})

                vals = {
                    'task_id': task_id,
                    'persona_id': persona.id,
                    'task_status': 'NotSubmitted',
                    'task_type': (item.get('task_type') or '').strip() or False,
                    'difficulty': (item.get('difficulty') or '').strip() or False,
                    'trajectory_modifier': (item.get('trajectory_modifier') or '').strip() or False,
                    'safety_critical': (item.get('safety_critical') or '').strip() or False,
                    'seed_prompt': item.get('seed_prompt', ''),
                    'initial_prompt': item.get('initial_prompt', ''),
                    'system_prompt': item.get('system_prompt', ''),
                    'agent_md': item.get('agent.md', ''),
                    'soul_md': item.get('soul.md', ''),
                    'memory_md': item.get('memory.md', ''),
                    'email': gmail_creds.get('email', ''),
                    'password': gmail_creds.get('password', ''),
                    'gog_auth': gog_auth_str,
                }

                domain_name = (item.get('domain') or '').strip()
                if domain_name:
                    taxonomy_ids = []
                    for tag in [t.strip() for t in domain_name.split(',') if t.strip()]:
                        tax = TaxonomyModel.search([('name', '=ilike', tag)], limit=1)
                        if not tax:
                            tax = TaxonomyModel.create({'name': tag})
                        taxonomy_ids.append(tax.id)
                    if taxonomy_ids:
                        vals['heart_taxonomy'] = [(6, 0, taxonomy_ids)]

                for service in ('outlook', 'eventbrite', 'strava', 'oura', 'instagram', 'facebook', 'threads'):
                    svc_creds = creds.get(service, {})
                    vals['%s_username' % service] = svc_creds.get('username') or svc_creds.get('email', '')
                    vals['%s_password' % service] = svc_creds.get('password', '')

                record = AtlasModel.create(vals)
                created_ids.append(record.id)

            return http.Response(
                json.dumps({'success': True, 'message': '%d records created' % len(created_ids), 'status': 200}),
                content_type='application/json',
                status=200
            )
        except Exception as e:
            _logger.exception("JSONL import failed")
            return http.Response(
                json.dumps({'error': 'Internal server error', 'status': 500}),
                content_type='application/json',
                status=500
            )