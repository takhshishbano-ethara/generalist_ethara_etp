from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass

import requests

from ..models import credential_manager
from . import cost

_logger = logging.getLogger(__name__)


_ANTHROPIC_KEY_PARAM = "lynceus.anthropic_api_key"
_SEED_PROMPT_FILE = pathlib.Path(__file__).resolve().parent.parent / "seed_prompts" / "image_gen_prompt_v1.md"

_seed_prompt_cache: str | None = None


@dataclass
class GenerationCall:
    tier: str | None = None
    archetype: str | None = None
    categories: list[str] | None = None
    seed: str | None = None


@dataclass
class GenerationResult:
    content: str
    usage: cost.TokenUsage
    cost_usd: float
    model: str


@dataclass
class APIConfig:
    api_key: str
    model: str
    base_url: str
    version: str = "2023-06-01"
    max_tokens: int = 300
    http_session: requests.Session | None = None


def load_seed_prompt() -> str:
    global _seed_prompt_cache
    if _seed_prompt_cache is None:
        if not _SEED_PROMPT_FILE.exists():
            raise FileNotFoundError(f"Lynceus seed prompt missing: {_SEED_PROMPT_FILE}")
        _seed_prompt_cache = _SEED_PROMPT_FILE.read_text(encoding="utf-8")
    return _seed_prompt_cache


def _build_user_message(call: GenerationCall) -> str:
    parts: list[str] = []
    if call.tier:
        parts.append(f"TIER={call.tier}")
    if call.archetype:
        parts.append(f"ARCHETYPE={call.archetype}")
    if call.categories:
        parts.append("CATEGORIES=" + ", ".join(call.categories))
    if call.seed:
        parts.append(f"SEED={call.seed}")
    if not parts:
        return "Generate one prompt."
    return "\n".join(parts)


def _get_api_key(env) -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(_ANTHROPIC_KEY_PARAM, "")
    if not stored:
        raise RuntimeError(
            "Lynceus: Anthropic API key not configured. "
            "Set it in Settings -> Lynceus."
        )
    return credential_manager.decrypt(ICP, stored)


def _get_settings(env) -> dict:
    ICP = env["ir.config_parameter"].sudo()
    return {
        "model": ICP.get_param("lynceus.anthropic_model", "claude-sonnet-4-6"),
        "base_url": ICP.get_param("lynceus.anthropic_base_url", "https://api.anthropic.com/v1/messages"),
        "version": ICP.get_param("lynceus.anthropic_version", "2023-06-01"),
        "max_tokens": int(ICP.get_param("lynceus.max_tokens_per_call", "300") or "300"),
    }


def build_config_from_env(env) -> APIConfig:
    settings = _get_settings(env)
    return APIConfig(
        api_key=_get_api_key(env),
        model=settings["model"],
        base_url=settings["base_url"],
        version=settings["version"],
        max_tokens=settings["max_tokens"],
    )


def _generate_one_pure(
    config: APIConfig,
    seed_prompt: str,
    call: GenerationCall,
    timeout: tuple[int, int] = (15, 60),
) -> GenerationResult:
    headers = {
        "x-api-key": config.api_key,
        "anthropic-version": config.version,
        "content-type": "application/json",
    }
    payload = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": seed_prompt,
        "messages": [{"role": "user", "content": _build_user_message(call)}],
    }
    poster = config.http_session.post if config.http_session else requests.post
    resp = poster(config.base_url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Anthropic API HTTP {resp.status_code}: {resp.text[:500]}"
        )
    body = resp.json()
    content_blocks = body.get("content") or []
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError(f"Anthropic API returned empty content: {json.dumps(body)[:500]}")
    usage_obj = body.get("usage") or {}
    usage = cost.TokenUsage(
        input_tokens=int(usage_obj.get("input_tokens", 0)),
        output_tokens=int(usage_obj.get("output_tokens", 0)),
    )
    return GenerationResult(
        content=text,
        usage=usage,
        cost_usd=cost.estimate_usd(config.model, usage),
        model=config.model,
    )


def generate_one(env, call: GenerationCall, timeout: tuple[int, int] = (15, 60)) -> GenerationResult:
    config = build_config_from_env(env)
    return _generate_one_pure(config, load_seed_prompt(), call, timeout)
