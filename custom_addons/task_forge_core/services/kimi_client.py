import json
import logging
import requests
from urllib.parse import quote
from odoo.http import request as odoo_request

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = 'https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse'


def _get_config(key, default=''):
    try:
        return odoo_request.env['ir.config_parameter'].sudo().get_param(key, default) or default
    except Exception:
        return default


def call_kimi(system_prompt, user_content, temperature=0.1):
    api_key = _get_config('task_forge_core.kimi_api_key')
    arn = _get_config('task_forge_core.kimi_model_arn')
    region = _get_config('task_forge_core.kimi_aws_region', 'us-east-1')

    if not api_key:
        raise ValueError("Bedrock API Key not configured. Go to Settings > Task Forge > Kimi K2.5 Configuration.")
    if not arn:
        raise ValueError("Bedrock Model ARN not configured. Go to Settings > Task Forge > Kimi K2.5 Configuration.")

    url = BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(arn, safe=''),
    )

    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % api_key,
    }

    payload = {
        'messages': [
            {'role': 'user', 'content': [{'text': user_content}]},
        ],
        'system': [
            {'text': system_prompt},
        ],
        'inferenceConfig': {
            'maxTokens': 4096,
            'temperature': temperature,
        },
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=120)

    if resp.status_code != 200:
        _logger.error('Bedrock API error (HTTP %d): %s', resp.status_code, resp.text[:500])
        raise RuntimeError('Bedrock API error (HTTP %d): %s' % (resp.status_code, resp.text[:300]))

    result = resp.json()

    output = result.get('output', {})
    message = output.get('message', {})
    content_blocks = message.get('content', [])
    text = ''
    for block in content_blocks:
        if 'text' in block:
            text += block['text']

    usage = result.get('usage', {})
    return {
        'text': text,
        'usage': {
            'input_tokens': usage.get('inputTokens', 0),
            'output_tokens': usage.get('outputTokens', 0),
        },
    }


def parse_json_response(text):
    if not text:
        return {}
    text = text.strip()
    if '```' in text:
        if '```json' in text:
            text = text.split('```json')[-1].split('```')[0]
        else:
            parts = text.split('```')
            text = parts[1] if len(parts) > 1 else parts[0]
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        return {}
