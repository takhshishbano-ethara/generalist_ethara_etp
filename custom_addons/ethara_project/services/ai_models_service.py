import logging

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEOUT = 15


class ProviderError(UserError):
    pass


def _normalize_provider_name(name):
    return "".join((name or "").split()).lower()


# Providers are often named with a vendor prefix in the admin ("Amazon
# Bedrock", "AWS Bedrock") — map those normalized names to the canonical
# fetcher key so the provider still resolves.
_PROVIDER_ALIASES = {
    "amazonbedrock": "bedrock",
    "awsbedrock": "bedrock",
    "bedrockruntime": "bedrock",
}


def _fetcher_key(name):
    """Normalized provider name resolved to its fetcher key (honoring aliases)."""
    normalized = _normalize_provider_name(name)
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _get_row(env, name):
    normalized = _normalize_provider_name(name)
    if not normalized:
        raise ProviderError("Provider name is required.")
    rows = env["ethara.project.ai.model"].sudo().search([("active", "=", True)])
    for row in rows:
        if _normalize_provider_name(row.name) == normalized:
            return row
    raise ProviderError(f"Provider '{name}' is not configured in AI Models.")


def _require_url(row):
    url = (row.api_url or "").strip()
    if not url:
        raise ProviderError(
            f"Missing API URL for {row.name}. Set it in Configuration \u2192 AI Models."
        )
    return url


def _require_key(row):
    key = row.sudo().api_key or ""
    if not key:
        raise ProviderError(
            f"Missing API key for {row.name}. Set it in Configuration \u2192 AI Models."
        )
    return key


def _openrouter(row):
    url = _require_url(row)
    headers = {}
    key = row.sudo().api_key or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for m in resp.json().get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append({"id": mid, "name": m.get("name") or mid})
    return out


def _openai(row):
    url = _require_url(row)
    key = _require_key(row)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for m in resp.json().get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append({"id": mid, "name": mid})
    return out


def _moonshot(row):
    url = _require_url(row)
    key = _require_key(row)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for m in resp.json().get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append({"id": mid, "name": mid})
    return out


def _gemini(row):
    url = _require_url(row)
    key = _require_key(row)
    resp = requests.get(
        url,
        params={"pageSize": 1000, "key": key},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for m in resp.json().get("models", []):
        mid = (m.get("name") or "").removeprefix("models/")
        if not mid:
            continue
        out.append({"id": mid, "name": m.get("displayName") or mid})
    return out


def _bedrock(row):
    creds = row.env['ethara.project.aws.credentials'].sudo().get_credentials()
    if not creds.get('aws_access_key_id') or not creds.get('aws_secret_access_key'):
        raise ProviderError(
            "AWS credentials missing. Set them in "
            "Settings → Ethara Projects → AWS Credentials."
        )
    try:
        import boto3
    except ImportError as exc:
        raise ProviderError(
            "boto3 is not installed on this Odoo server."
        ) from exc
    try:
        client = boto3.client('bedrock', **creds)
        resp = client.list_foundation_models()
    except Exception as exc:
        raise ProviderError(f"Bedrock unreachable: {exc}") from exc
    out = []
    for m in resp.get('modelSummaries', []):
        mid = m.get('modelId') or ''
        if not mid:
            continue
        provider = m.get('providerName') or ''
        model_name = m.get('modelName') or mid
        display = f"{provider} \u00b7 {model_name}".strip(' \u00b7')
        out.append({"id": mid, "name": display})
    return out


_FETCHERS = {
    "openrouter": _openrouter,
    "openai": _openai,
    "moonshot": _moonshot,
    "gemini": _gemini,
    "bedrock": _bedrock,
}


def supported_providers():
    return sorted(_FETCHERS.keys())


def list_models(env, provider_name):
    row = _get_row(env, provider_name)
    fetcher = _FETCHERS.get(_fetcher_key(row.name))
    if not fetcher:
        raise ProviderError(
            f"Provider '{row.name}' is not supported. "
            f"Supported: OpenRouter, OpenAI, Moonshot, Gemini, Bedrock."
        )
    try:
        models = fetcher(row)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (401, 403):
            raise ProviderError(
                f"Invalid API key for {row.name}. Check the key in Configuration \u2192 AI Models."
            ) from exc
        if status == 429:
            raise ProviderError(
                f"Rate limited by {row.name}. Try again in a moment."
            ) from exc
        raise ProviderError(
            f"{row.name} returned HTTP {status}. Try again later."
        ) from exc
    except requests.RequestException as exc:
        raise ProviderError(
            f"{row.name} is unreachable: {exc}"
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        _logger.exception("AI Models: unexpected response shape from %s", row.name)
        raise ProviderError(
            f"{row.name} returned an unexpected response shape."
        ) from exc
    models.sort(key=lambda m: (m.get("name") or "").lower())
    return {"provider": row.name, "count": len(models), "models": models}


def list_providers(env):
    rows = env["ethara.project.ai.model"].sudo().search([("active", "=", True)])
    supported = set(_FETCHERS.keys())
    out = []
    for row in rows:
        if _fetcher_key(row.name) not in supported:
            continue
        out.append({"id": row.id, "name": row.name})
    return out
