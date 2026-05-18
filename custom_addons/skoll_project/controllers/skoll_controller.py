# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import random
import requests
import os
from dotenv import load_dotenv
import logging

_logger = logging.getLogger(__name__)

load_dotenv()


class Skoll(http.Controller):
    @http.route('/api/skoll/get_jsonl_data', type='http', auth='public', methods=['POST'], csrf=False, cors='*')
    def method_get_jsonl_data(self, **params):
        try:
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except:
                try:
                    jdata = json.loads(request.httprequest.data)
                except:
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

            response = requests.get(url, timeout=60)
            response.raise_for_status()  # fail fast if error

            data = []
            for line in response.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Fix malformed JSON: "gog_auth"{ -> "gog_auth":{
                line = line.replace('"gog_auth"{', '"gog_auth":{')
                # Strip trailing commas
                line = line.rstrip(',')
                # Balance unmatched braces
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

            SkollModel = request.env['skoll.skoll'].sudo()
            PersonaModel = request.env['skoll.persona'].sudo()
            DomainModel = request.env['skoll.domain'].sudo()
            TaxonomyModel = request.env['skoll.taxonomy'].sudo()

            created_ids = []
            for item in data:
                task_id = item.get('id', '')

                # Skip duplicates
                if task_id and SkollModel.search([('task_id', '=', task_id)], limit=1):
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

                TAG_FIELDS = (
                    ('life_domain', 'life_domain_ids', 'skoll.tag.life_domain'),
                    ('cluster', 'cluster_ids', 'skoll.tag.cluster'),
                    ('task_type', 'task_type_ids', 'skoll.tag.task_type'),
                    ('pattern_taxonomy', 'pattern_taxonomy_ids', 'skoll.tag.pattern_taxonomy'),
                )
                for jsonl_key, model_field, tag_model in TAG_FIELDS:
                    raw = (item.get(jsonl_key) or '').strip()
                    if not raw:
                        continue
                    TagModel = request.env[tag_model].sudo()
                    tag_ids = []
                    for part in raw.split(','):
                        tag_name = part.strip()
                        if not tag_name:
                            continue
                        tag = TagModel.search([('name', '=ilike', tag_name)], limit=1)
                        if not tag:
                            tag = TagModel.create({'name': tag_name})
                        tag_ids.append(tag.id)
                    if tag_ids:
                        vals[model_field] = [(6, 0, tag_ids)]

                for service in ('outlook', 'eventbrite', 'strava', 'oura', 'instagram', 'facebook', 'threads'):
                    svc_creds = creds.get(service, {})
                    vals['%s_username' % service] = svc_creds.get('username') or svc_creds.get('email', '')
                    vals['%s_password' % service] = svc_creds.get('password', '')

                record = SkollModel.create(vals)
                created_ids.append(record.id)

            return http.Response(
                json.dumps({'success': True, 'message': '%d records created' % len(created_ids), 'status': 200}),
                content_type='application/json',
                status=200
            )
        except Exception as e:
            return http.Response(
                json.dumps({'error': str(e), 'status': 500}),
                content_type='application/json',
                status=500
            )
