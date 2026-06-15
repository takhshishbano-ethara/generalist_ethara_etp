# -*- coding: utf-8 -*-
"""Image generation for etp_assessment — Vertex AI (single-provider stack).

Generates the A/B images for image_comparison questions from text prompts,
using Google Vertex AI Imagen on the SAME project/key as the text + scoring
Gemini model. One Vertex stack for generation, image-gen, and scoring.

System Parameters (Settings > Technical > System Parameters):
  etp_assessment.vertex_project_id    (shared)
  etp_assessment.vertex_api_key       (shared)
  etp_assessment.vertex_location      (shared, default us-central1)
  etp_assessment.vertex_image_model   image model id
                                      (default imagen-4.0-generate-001;
                                       empty = image gen disabled)
"""
import base64
import logging

_logger = logging.getLogger(__name__)


def _img_creds(env):
    from .vertex_questions import _param
    return (
        _param(env, "etp_assessment.vertex_project_id"),
        _param(env, "etp_assessment.vertex_location", "us-central1"),
        _param(env, "etp_assessment.vertex_image_model",
               "imagen-4.0-generate-001"),
        _param(env, "etp_assessment.vertex_api_key"),
    )


def _bearer(env):
    from .vertex_questions import _param
    return _param(env, "etp_assessment.vertex_access_token")


def is_configured(env):
    _project, _loc, model, api_key = _img_creds(env)
    return bool(model and (api_key or _bearer(env)))


def generate_image_b64(env, prompt, width=1024, height=1024):
    """One Vertex Imagen predict call -> base64 PNG (Odoo Binary-ready).

    Raises ValueError/RuntimeError on misconfig/API failure so callers can
    mark the draft image_state='failed' and retry.
    """
    import httpx

    project, location, model, api_key = _img_creds(env)
    if not model or not (api_key or _bearer(env)):
        raise ValueError(
            "Vertex image generation not configured. Set "
            "etp_assessment.vertex_image_model and either vertex_api_key "
            "(Gemini Developer API) or vertex_access_token + vertex_project_id "
            "(Vertex AI OAuth) in System Parameters.")

    # auth-aware endpoint (same logic as the text model): API key -> Gemini
    # Developer API; OAuth bearer -> Vertex aiplatform. Mixing key + aiplatform
    # is the 401/403 trap we avoid.
    from .vertex_questions import _gemini_request
    url, headers = _gemini_request(env, model, "predict")
    payload = {
        "instances": [{"prompt": prompt[:1024]}],
        "parameters": {"sampleCount": 1, "aspectRatio": "1:1"},
    }

    _logger.info(
        "etp_assessment Vertex image call: model=%s prompt_len=%d",
        model, len(prompt))
    with httpx.Client(
        timeout=httpx.Timeout(connect=30, read=180, write=30, pool=30)
    ) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Vertex image error [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json()
    preds = data.get("predictions") or []
    if not preds:
        raise RuntimeError(
            f"Vertex image response had no predictions: {str(data)[:300]}")
    # Imagen returns bytesBase64Encoded — exactly what Odoo Binary stores.
    b64 = preds[0].get("bytesBase64Encoded") or preds[0].get("image")
    if not b64:
        raise RuntimeError(
            f"Vertex image prediction missing image bytes: {str(preds[0])[:200]}")
    return b64
