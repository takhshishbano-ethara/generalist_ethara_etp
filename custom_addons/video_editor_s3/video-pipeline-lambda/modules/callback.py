import hashlib
import hmac
import json
import logging
import os
import urllib.request

import config

_logger = logging.getLogger(__name__)
_token_cache = {"value": None}


def _resolve_token():
    if _token_cache["value"] is not None:
        return _token_cache["value"]
    inline = os.environ.get("WEBHOOK_TOKEN")
    if inline:
        _token_cache["value"] = inline
        return inline
    arn = config.WEBHOOK_TOKEN_SECRET_ARN
    if not arn:
        _token_cache["value"] = ""
        return ""
    import boto3
    client = boto3.client("secretsmanager", region_name=config.S3_REGION)
    resp = client.get_secret_value(SecretId=arn)
    value = resp.get("SecretString") or ""
    _token_cache["value"] = value
    return value


def post(url, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _resolve_token()
    if token:
        sig = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Video-Pipeline-Token"] = sig
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.CALLBACK_TIMEOUT_SECONDS) as resp:
            return resp.status
    except Exception as exc:
        _logger.warning("callback POST to %s failed: %s", url, exc)
        return None
