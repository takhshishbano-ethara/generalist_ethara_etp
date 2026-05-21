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


def _detect_media_type(url, content_bytes=None):
    if content_bytes:
        if content_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return 'image/png'
        if content_bytes[:4] == b'RIFF' and content_bytes[8:12] == b'WEBP':
            return 'image/webp'
        if content_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return 'image/gif'
    url_lower = url.lower()
    if '.png' in url_lower:
        return 'image/png'
    if '.webp' in url_lower:
        return 'image/webp'
    if '.gif' in url_lower:
        return 'image/gif'
    return 'image/jpeg'


def fetch_image_as_base64(url):
    import base64
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        _logger.warning('Failed to fetch image from %s (HTTP %d)', url, resp.status_code)
        return None, None
    media_type = _detect_media_type(url, content_bytes=resp.content)
    b64 = base64.b64encode(resp.content).decode('utf-8')
    return b64, media_type


def call_kimi(system_prompt, user_content, temperature=0.1, images=None):
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

    content_blocks = [{'text': user_content}]
    if images:
        for img in images:
            content_blocks.append({
                'image': {
                    'format': img['format'],
                    'source': {'bytes': img['base64']},
                }
            })

    payload = {
        'messages': [
            {'role': 'user', 'content': content_blocks},
        ],
        'system': [
            {'text': system_prompt},
        ],
        'inferenceConfig': {
            'maxTokens': 16384,
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
    """Best-effort JSON extraction from a chat-completion reply.

    Order of attempts:
    1. Strip whitespace + parse as-is (the strict prompt SHOULD give us
       a bare JSON object).
    2. If the model wrapped it in ``` fences (with or without a "json"
       language tag), unwrap the first fenced block.
    3. Scan for the first ``{`` that opens a balanced object — this
       handles preambles like ``Here is the JSON:`` and trailing
       commentary the model sometimes adds despite the system prompt
       telling it not to.

    Returns the parsed dict on success, or ``{}`` if nothing parsable
    was found.
    """
    if not text:
        return {}
    text = text.strip()

    # Pass 1: the strict-prompt happy path — full reply is the object.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Pass 2: unwrap a fenced block.  Handles ```json, ```JSON, plain
    # ``` with no tag, and stray whitespace after the opening fence.
    import re
    fence = re.search(
        r"```(?:json|JSON)?\s*\n?(.*?)```",
        text,
        re.DOTALL,
    )
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    # Pass 3: find the first balanced ``{...}`` by walking braces.
    # ``re.search(r'\{.*\}', text, re.DOTALL)`` from the old impl was
    # greedy and gobbled trailing commentary INTO the JSON candidate,
    # then failed.  Manual balancing is safer for nested objects.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:idx + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break  # try next `{`
        start = text.find("{", start + 1)

    return {}
