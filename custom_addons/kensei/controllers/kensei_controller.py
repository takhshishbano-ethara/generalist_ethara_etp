# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging

_logger = logging.getLogger(__name__)


class KenseiController(http.Controller):

    @http.route('/api/kensei/upload_tasks', type='http', auth='public',
                methods=['POST'], csrf=False, cors='*')
    def upload_tasks_jsonl(self, **params):
        """
        Upload Kensei tasks from a JSONL source.

        Accepts:
          A) {"url": "https://..."}  — fetches JSONL from remote URL
          B) {"tasks": [...]}        — inline array of task objects

        Task object fields:
          id, persona, initial_prompt (→seed_prompt), system_prompt,
          mm_taxonomy.L1, mm_taxonomy.L2, agent.md, soul.md, memory.md,
          gog_auth, credentials.email, credentials.<service>.username/.password

        Returns: {"success": true, "created": N, "skipped": N, "errors": [...]}
        """
        import requests as http_requests

        try:
            try:
                body = json.loads(request.httprequest.stream.read())
            except Exception:
                try:
                    body = json.loads(request.httprequest.data)
                except Exception:
                    body = {}

            data = []
            if 'url' in body and body['url']:
                url = str(body['url']).strip()
                if url.startswith('/') or url.startswith('file://'):
                    file_path = url.replace('file://', '')
                    with open(file_path, 'r', encoding='utf-8') as f:
                        raw_text = f.read()
                else:
                    resp = http_requests.get(url, timeout=120)
                    resp.raise_for_status()
                    raw_text = resp.text
                for line in raw_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    line = line.replace('"gog_auth"{', '"gog_auth":{')
                    line = line.rstrip(',')
                    diff = line.count('{') - line.count('}')
                    if diff > 0:
                        line += '}' * diff
                    try:
                        data.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        _logger.warning("Skipping malformed JSONL line: %s", str(e)[:200])
                        continue
            elif 'tasks' in body and isinstance(body['tasks'], list):
                data = body['tasks']
            else:
                return http.Response(
                    json.dumps({'error': 'Provide "url" (JSONL source) or "tasks" (array)', 'status': 400}),
                    content_type='application/json', status=400
                )

            if not data:
                return http.Response(
                    json.dumps({'error': 'No valid task data found', 'status': 400}),
                    content_type='application/json', status=400
                )

            Kensei = request.env['kensei.kensei'].sudo()
            Persona = request.env['kensei.persona'].sudo()
            Domain = request.env['kensei.domain'].sudo()

            created = 0
            skipped = 0
            errors = []

            for idx, item in enumerate(data):
                try:
                    task_id = (item.get('id') or '').strip()
                    if not task_id:
                        errors.append({'index': idx, 'error': 'Missing "id" field'})
                        continue

                    if Kensei.search([('task_id', '=', task_id)], limit=1):
                        skipped += 1
                        continue

                    persona_name = (item.get('persona') or '').strip()
                    if persona_name:
                        persona = Persona.search([('name', '=ilike', persona_name)], limit=1)
                        if not persona:
                            persona = Persona.create({
                                'name': persona_name,
                                'soul_md': item.get('soul.md', ''),
                                'memory_md': item.get('memory.md', ''),
                                'agents_md': item.get('agent.md', ''),
                            })
                    else:
                        persona = Persona.search([], limit=1)
                        if not persona:
                            persona = Persona.create({'name': 'default'})

                    l1_id = False
                    l2_id = False
                    mm_taxonomy = item.get('mm_taxonomy', {})
                    l1_name = (mm_taxonomy.get('L1') or '').strip()
                    l2_name = (mm_taxonomy.get('L2') or '').strip()

                    if l1_name:
                        l1_domain = Domain.search([
                            ('name', '=ilike', l1_name),
                            ('parent_id', '=', False),
                        ], limit=1)
                        if not l1_domain:
                            l1_domain = Domain.create({'name': l1_name, 'parent_id': False})
                        l1_id = l1_domain.id

                        if l2_name:
                            l2_domain = Domain.search([
                                ('name', '=ilike', l2_name),
                                ('parent_id', '=', l1_domain.id),
                            ], limit=1)
                            if not l2_domain:
                                l2_domain = Domain.create({'name': l2_name, 'parent_id': l1_domain.id})
                            l2_id = l2_domain.id

                    creds = item.get('credentials', {})
                    gmail_creds = creds.get('gmail', {})
                    email = (creds.get('email') or gmail_creds.get('email') or '').strip()
                    password = (gmail_creds.get('password') or '').strip()

                    gog_auth_val = item.get('gog_auth')
                    gog_auth_str = json.dumps(gog_auth_val) if gog_auth_val else ''

                    vals = {
                        'task_id': task_id,
                        'persona_id': persona.id,
                        'task_status': 'NotSubmitted',
                        'seed_prompt': item.get('initial_prompt', ''),
                        'initial_prompt': item.get('initial_prompt', ''),
                        'system_prompt': item.get('system_prompt', ''),
                        'agent_md': item.get('agent.md', ''),
                        'soul_md': item.get('soul.md', ''),
                        'memory_md': item.get('memory.md', ''),
                        'email': email,
                        'password': password,
                        'gog_auth': gog_auth_str,
                        'l1_classification': l1_id,
                        'l2_classification': l2_id,
                    }

                    for service in ('outlook', 'eventbrite', 'strava', 'oura',
                                    'instagram', 'facebook', 'threads'):
                        svc_creds = creds.get(service, {})
                        vals['%s_username' % service] = (
                            svc_creds.get('username') or svc_creds.get('email') or ''
                        )
                        vals['%s_password' % service] = svc_creds.get('password', '')

                    Kensei.create(vals)
                    created += 1

                except Exception as e:
                    _logger.exception("Error creating task at index %d", idx)
                    errors.append({'index': idx, 'id': item.get('id', ''), 'error': str(e)})

            result = {
                'success': True,
                'created': created,
                'skipped': skipped,
                'total_in_file': len(data),
                'status': 200,
            }
            if errors:
                result['errors'] = errors

            return http.Response(
                json.dumps(result),
                content_type='application/json', status=200
            )

        except Exception as e:
            _logger.exception("JSONL upload failed")
            return http.Response(
                json.dumps({'error': str(e), 'status': 500}),
                content_type='application/json', status=500
            )

            if not data:
                return http.Response(
                    json.dumps({'error': 'No valid task data found', 'status': 400}),
                    content_type='application/json', status=400
                )

            # --- Models ---
            Kensei = request.env['kensei.kensei'].sudo()
            Persona = request.env['kensei.persona'].sudo()
            Domain = request.env['kensei.domain'].sudo()

            created = 0
            skipped = 0
            errors = []

            for idx, item in enumerate(data):
                try:
                    task_id = (item.get('id') or '').strip()
                    if not task_id:
                        errors.append({'index': idx, 'error': 'Missing "id" field'})
                        continue

                    # Skip duplicates
                    if Kensei.search([('task_id', '=', task_id)], limit=1):
                        skipped += 1
                        continue

                    # --- Persona ---
                    persona_name = (item.get('persona') or '').strip()
                    if persona_name:
                        persona = Persona.search([('name', '=ilike', persona_name)], limit=1)
                        if not persona:
                            persona = Persona.create({
                                'name': persona_name,
                                'soul_md': item.get('soul.md', ''),
                                'memory_md': item.get('memory.md', ''),
                                'agents_md': item.get('agent.md', ''),
                            })
                    else:
                        persona = Persona.search([], limit=1)
                        if not persona:
                            persona = Persona.create({'name': 'default'})

                    # --- L1 / L2 Classification ---
                    l1_id = False
                    l2_id = False
                    mm_taxonomy = item.get('mm_taxonomy', {})
                    l1_name = (mm_taxonomy.get('L1') or '').strip()
                    l2_name = (mm_taxonomy.get('L2') or '').strip()

                    if l1_name:
                        l1_domain = Domain.search([
                            ('name', '=ilike', l1_name),
                            ('parent_id', '=', False),
                        ], limit=1)
                        if not l1_domain:
                            # Auto-create L1 domain
                            l1_domain = Domain.create({'name': l1_name, 'parent_id': False})
                        l1_id = l1_domain.id

                        if l2_name:
                            l2_domain = Domain.search([
                                ('name', '=ilike', l2_name),
                                ('parent_id', '=', l1_domain.id),
                            ], limit=1)
                            if not l2_domain:
                                # Auto-create L2 under L1
                                l2_domain = Domain.create({'name': l2_name, 'parent_id': l1_domain.id})
                            l2_id = l2_domain.id

                    # --- Credentials ---
                    creds = item.get('credentials', {})
                    gmail_creds = creds.get('gmail', {})
                    # Email: try credentials.email (top-level), then gmail.email
                    email = (creds.get('email') or gmail_creds.get('email') or '').strip()
                    password = (gmail_creds.get('password') or '').strip()

                    # --- GOG Auth ---
                    gog_auth_val = item.get('gog_auth')
                    gog_auth_str = json.dumps(gog_auth_val) if gog_auth_val else ''

                    # --- Build record values ---
                    vals = {
                        'task_id': task_id,
                        'persona_id': persona.id,
                        'task_status': 'NotSubmitted',
                        'seed_prompt': item.get('initial_prompt', ''),
                        'initial_prompt': item.get('initial_prompt', ''),
                        'system_prompt': item.get('system_prompt', ''),
                        'agent_md': item.get('agent.md', ''),
                        'soul_md': item.get('soul.md', ''),
                        'memory_md': item.get('memory.md', ''),
                        'email': email,
                        'password': password,
                        'gog_auth': gog_auth_str,
                        'l1_classification': l1_id,
                        'l2_classification': l2_id,
                    }

                    # --- Service credentials ---
                    for service in ('outlook', 'eventbrite', 'strava', 'oura',
                                    'instagram', 'facebook', 'threads'):
                        svc_creds = creds.get(service, {})
                        vals['%s_username' % service] = (
                            svc_creds.get('username') or svc_creds.get('email') or ''
                        )
                        vals['%s_password' % service] = svc_creds.get('password', '')

                    Kensei.create(vals)
                    created += 1

                except Exception as e:
                    _logger.exception("Error creating task at index %d", idx)
                    errors.append({'index': idx, 'id': item.get('id', ''), 'error': str(e)})

            result = {
                'success': True,
                'created': created,
                'skipped': skipped,
                'total_in_file': len(data),
                'status': 200,
            }
            if errors:
                result['errors'] = errors

            return http.Response(
                json.dumps(result),
                content_type='application/json', status=200
            )

        except Exception as e:
            _logger.exception("JSONL upload failed")
            return http.Response(
                json.dumps({'error': str(e), 'status': 500}),
                content_type='application/json', status=500
            )
