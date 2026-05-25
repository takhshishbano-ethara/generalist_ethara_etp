# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging
import os

_logger = logging.getLogger(__name__)


class Kensei2Controller(http.Controller):

    @http.route('/api/kensei2/upload_tasks', type='http', auth='public',
                methods=['POST'], csrf=False, cors='*')
    def upload_tasks_jsonl(self, **params):
        """
        Upload Kensei2 tasks from a JSONL source.

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

            Kensei2 = request.env['kensei2.kensei2'].sudo()
            Persona = request.env['kensei2.persona'].sudo()
            Domain = request.env['kensei2.domain'].sudo()

            created = 0
            skipped = 0
            errors = []

            for idx, item in enumerate(data):
                try:
                    task_id = (item.get('id') or '').strip()
                    if not task_id:
                        errors.append({'index': idx, 'error': 'Missing "id" field'})
                        continue

                    if Kensei2.search([('task_id', '=', task_id)], limit=1):
                        skipped += 1
                        continue

                    persona_name = (item.get('persona') or '').strip()
                    if persona_name:
                        # Normalize to match model's create/write (lowercase, spaces→hyphens)
                        normalized_name = persona_name.strip().lower().replace(" ", "-")
                        persona = Persona.search([('name', '=ilike', normalized_name)], limit=1)
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

                    # --- Credentials ---
                    creds = item.get('credentials', {})
                    gmail_creds = creds.get('gmail', {})
                    email = (creds.get('email') or gmail_creds.get('email') or '').strip()
                    password = (creds.get('password') or gmail_creds.get('password') or '').strip()

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

                    # --- Service credentials ---
                    for service in ('outlook', 'eventbrite', 'strava', 'oura',
                                    'instagram', 'facebook', 'threads'):
                        svc_creds = creds.get(service, {})
                        vals['%s_username' % service] = (
                            svc_creds.get('username') or svc_creds.get('email') or ''
                        )
                        vals['%s_password' % service] = svc_creds.get('password', '')

                    Kensei2.create(vals)
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

    @http.route('/kensei2/harbor/download/<int:task_id>', type='http', auth='user', methods=['GET'])
    def download_harbor_task(self, task_id, **kwargs):
        import io
        import zipfile
        import boto3
        from botocore.config import Config as BotoConfig

        user = request.env.user
        is_ql = user.has_group('kensei2.group_kensei2_ql')
        is_pl = user.has_group('kensei2.group_kensei2_pl')
        if not (is_ql or is_pl):
            return http.Response(
                json.dumps({'error': 'Access denied. QL or PL role required.'}),
                content_type='application/json', status=403
            )

        task = request.env["kensei2.kensei2"].sudo().browse(task_id)
        if not task.exists():
            return http.Response(
                json.dumps({'error': 'Task not found.'}),
                content_type='application/json', status=404
            )

        icp = request.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or ""
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or "Kensei2"
        task_name = task.task_id or str(task_id)
        task_prefix = "%s/harbor/%s/" % (prefix, task_name)

        if not bucket:
            return http.Response(
                json.dumps({'error': 'S3 bucket not configured.'}),
                content_type='application/json', status=400
            )

        access_key = os.environ.get("KENSEI2_S3_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_KEY", "")
        secret_key = os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_SECRET_KEY", "")

        client_kwargs = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
        }
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        try:
            s3 = boto3.client("s3", **client_kwargs)
            all_keys = []
            continuation_token = None
            while True:
                list_kwargs = {"Bucket": bucket, "Prefix": task_prefix, "MaxKeys": 1000}
                if continuation_token:
                    list_kwargs["ContinuationToken"] = continuation_token
                resp = s3.list_objects_v2(**list_kwargs)
                all_keys.extend(resp.get("Contents", []))
                if resp.get("IsTruncated"):
                    continuation_token = resp.get("NextContinuationToken")
                else:
                    break

            if not all_keys:
                return http.Response(
                    json.dumps({'error': 'No Harbor export found for this task. Export first.'}),
                    content_type='application/json', status=404
                )

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for obj in all_keys:
                    key = obj["Key"]
                    rel_path = key[len(task_prefix):]
                    if not rel_path:
                        continue
                    try:
                        file_resp = s3.get_object(Bucket=bucket, Key=key)
                        zf.writestr(rel_path, file_resp["Body"].read())
                    except Exception:
                        _logger.warning("Failed to fetch s3://%s/%s, skipping", bucket, key)

            buf.seek(0)
            zip_data = buf.getvalue()
            filename = "harbor_%s.zip" % task_name
            headers = [
                ('Content-Type', 'application/zip'),
                ('Content-Disposition', 'attachment; filename="%s"' % filename),
                ('Content-Length', str(len(zip_data))),
            ]
            return request.make_response(zip_data, headers=headers)
        except Exception as e:
            _logger.exception("Harbor per-task download failed for task %s", task_id)
            return http.Response(
                json.dumps({'error': 'Download failed: %s' % str(e)}),
                content_type='application/json', status=500
            )

    @http.route('/kensei2/harbor/download', type='http', auth='user', methods=['GET'])
    def download_harbor_bundle(self, **kwargs):
        import io
        import zipfile
        import boto3
        from botocore.config import Config as BotoConfig

        user = request.env.user
        is_ql = user.has_group('kensei2.group_kensei2_ql')
        is_pl = user.has_group('kensei2.group_kensei2_pl')
        if not (is_ql or is_pl):
            return http.Response(
                json.dumps({'error': 'Access denied. QL or PL role required.'}),
                content_type='application/json', status=403
            )

        icp = request.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or ""
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or "Kensei2"

        if not bucket:
            return http.Response(
                json.dumps({'error': 'S3 bucket not configured. Go to Settings → Kensei2.'}),
                content_type='application/json', status=400
            )

        harbor_prefix = "%s/harbor/" % prefix

        access_key = os.environ.get("KENSEI2_S3_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_KEY", "")
        secret_key = os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_SECRET_KEY", "")

        client_kwargs = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
        }
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        try:
            s3 = boto3.client("s3", **client_kwargs)

            all_keys = []
            continuation_token = None
            while True:
                list_kwargs = {"Bucket": bucket, "Prefix": harbor_prefix, "MaxKeys": 1000}
                if continuation_token:
                    list_kwargs["ContinuationToken"] = continuation_token
                resp = s3.list_objects_v2(**list_kwargs)
                contents = resp.get("Contents", [])
                all_keys.extend(contents)
                if resp.get("IsTruncated"):
                    continuation_token = resp.get("NextContinuationToken")
                else:
                    break

            if not all_keys:
                return http.Response(
                    json.dumps({'error': 'No Harbor tasks found in S3. Export some tasks first.'}),
                    content_type='application/json', status=404
                )

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for obj in all_keys:
                    key = obj["Key"]
                    rel_path = key[len(harbor_prefix):]
                    if not rel_path:
                        continue
                    try:
                        file_resp = s3.get_object(Bucket=bucket, Key=key)
                        file_data = file_resp["Body"].read()
                        zf.writestr(rel_path, file_data)
                    except Exception:
                        _logger.warning("Failed to fetch s3://%s/%s, skipping", bucket, key)
                        continue

            buf.seek(0)
            zip_data = buf.getvalue()

            headers = [
                ('Content-Type', 'application/zip'),
                ('Content-Disposition', 'attachment; filename="harbor_tasks.zip"'),
                ('Content-Length', str(len(zip_data))),
            ]
            return request.make_response(zip_data, headers=headers)

        except Exception as e:
            _logger.exception("Harbor download failed")
            return http.Response(
                json.dumps({'error': 'S3 download failed: %s' % str(e)}),
                content_type='application/json', status=500
            )
