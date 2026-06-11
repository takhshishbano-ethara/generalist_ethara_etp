# -*- coding: utf-8 -*-
"""Bedrock image-generation service for etp_assessment.

Generates the A/B images for `image_comparison` questions produced by the
LLM question generator. The question-generation LLM emits two image PROMPTS
per image_comparison question; this service turns each prompt into an actual
image via Bedrock's image models (Titan Image Generator / Nova Canvas — both
speak the same invoke body shape used below).

System Parameters (all under Settings > Technical > System Parameters):
  etp_assessment.bedrock_region          (shared with question gen / scoring)
  etp_assessment.bedrock_bearer_token    (shared)
  etp_assessment.bedrock_image_model_id  e.g. "amazon.nova-canvas-v1:0" or
                                         "amazon.titan-image-generator-v2:0"
                                         (empty = image gen disabled)
"""
import json
import logging
import urllib.parse

_logger = logging.getLogger(__name__)


def _image_creds(env):
    from .bedrock_questions import _param
    return (
        _param(env, "etp_assessment.bedrock_image_model_id"),
        _param(env, "etp_assessment.bedrock_region", "us-east-1"),
        _param(env, "etp_assessment.bedrock_bearer_token"),
    )


def is_configured(env):
    model_id, _region, token = _image_creds(env)
    return bool(model_id and token)


def generate_image_b64(env, prompt, width=1024, height=1024):
    """One Bedrock invoke call -> base64 PNG string (Odoo Binary-ready).

    Raises RuntimeError/ValueError on misconfiguration or API failure so the
    caller can mark the draft question's image_state='failed' and retry later.
    """
    import httpx

    model_id, region, token = _image_creds(env)
    if not model_id or not token:
        raise ValueError(
            "Bedrock image generation not configured. Set "
            "etp_assessment.bedrock_image_model_id and "
            "etp_assessment.bedrock_bearer_token in System Parameters."
        )

    endpoint = (
        f"https://bedrock-runtime.{region}.amazonaws.com/model/"
        f"{urllib.parse.quote(model_id, safe='')}/invoke"
    )
    # Titan Image Generator and Nova Canvas share this request schema.
    payload = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt[:1024]},
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width": width,
            "height": height,
            "quality": "standard",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    _logger.info(
        "etp_assessment Bedrock image call: model=%s region=%s prompt_len=%d",
        model_id, region, len(prompt),
    )
    with httpx.Client(
        timeout=httpx.Timeout(connect=30, read=180, write=30, pool=30)
    ) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Bedrock image error [{resp.status_code}]: {resp.text[:400]}"
        )
    data = resp.json()
    images = data.get("images") or []
    if not images:
        raise RuntimeError(
            f"Bedrock image response contained no images: {json.dumps(data)[:300]}"
        )
    # Already base64 — exactly what Odoo Binary fields store.
    return images[0]
