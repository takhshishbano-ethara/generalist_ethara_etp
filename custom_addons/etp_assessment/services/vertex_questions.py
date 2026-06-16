# -*- coding: utf-8 -*-
"""Vertex AI Gemini question-generation service for etp_assessment.

Single-provider stack: Vertex AI Gemini for text, Imagen for images.
Reads credentials from System Parameters (anything containing PLACEHOLDER
is treated as unset by `_param`):

  etp_assessment.vertex_project_id
  etp_assessment.vertex_location      (default us-central1)
  etp_assessment.vertex_model         (default gemini-3-pro)
  etp_assessment.vertex_api_key       (Gemini Developer API key — `AIza...`)
  etp_assessment.vertex_access_token  (Vertex AI OAuth Bearer; expires ~1h)

Auth routing (see `_gemini_request`): bearer wins when both are set and
goes to `aiplatform.googleapis.com`; api_key goes to
`generativelanguage.googleapis.com`. Mixing them on the wrong endpoint
returns 401/403 — never paste an `AIza` key into the access_token slot.
"""
import json
import logging
import re
import urllib.parse

_logger = logging.getLogger(__name__)

DEFAULT_SKILLS_PROMPT = (
    "You are an expert assessment designer. Given a project's SOP or instruction "
    "text, identify the distinct SKILLS a candidate must be tested on to work on "
    "this project. Return ONLY a JSON array, no prose. Each item: "
    '{"skill": "<short skill name>", "reason": "<one line why it matters>", '
    '"suggested_max": <int 3-10>}. Extract 4-10 skills.'
)

DEFAULT_QUESTIONS_PROMPT = (
    "You are an expert assessment author. Generate assessment questions for the "
    "given list of SKILLS, grounded in the provided project text. For each skill "
    "produce exactly the requested number of questions. Return ONLY a JSON array, "
    "no prose. Each item: "
    '{"skill": "<skill name exactly as given>", "title": "<short question title>", '
    '"prompt": "<the full question text>", "type": "text"}. '
    'For visual-evaluation skills you may instead emit "type": "image_comparison" '
    "items; those MUST additionally include "
    '"image_prompt_a": "<text-to-image prompt for response A>" and '
    '"image_prompt_b": "<text-to-image prompt for response B, a deliberately '
    'different/flawed variant of A>". '
    "Make questions concrete, project-relevant, and non-trivial."
)


def _param(env, key, default=""):
    """get_param that treats seeded PLACEHOLDER_* values as unset."""
    val = env["ir.config_parameter"].sudo().get_param(key, default) or default
    if isinstance(val, str) and "PLACEHOLDER" in val:
        return default
    return val


def _load_bundled_prompt(filename):
    """Read a markdown prompt from the module's prompts/ folder.

    Returns the file contents on success, '' on any failure. Used as the
    second tier of prompt resolution: System Parameter wins, then this
    bundled default, then the terse inline DEFAULT_* constants.

    Allowing the research team to ship full-length markdown prompts as
    files alongside the code keeps the Python source readable AND lets
    the prompt evolve in version control without touching the inline
    constants (which exist only as a last-ditch fallback).
    """
    import os
    try:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", filename,
        )
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        _logger.warning(
            "etp_assessment: could not load bundled prompt %s: %s",
            filename, exc)
        return ""


DEFAULT_SEED_PROMPT = (
    "You are an expert assessment author. You will receive a project SOP and "
    "ONE golden example question that demonstrates the desired style, depth "
    "and format. Generate a set of assessment questions grounded in the SOP, "
    "matching the golden example's quality and shape. Return ONLY a JSON "
    "array, no prose. Each item: "
    '{"title": "<short question title>", "prompt": "<the full question text>", '
    '"type": "text"}. '
    'For visual-evaluation content you may emit "type": "image_comparison" '
    "items; those MUST additionally include "
    '"image_prompt_a": "<text-to-image prompt for response A>" and '
    '"image_prompt_b": "<text-to-image prompt for response B, a deliberately '
    'different/flawed variant of A>". '
    "Make questions concrete, project-relevant, and non-trivial."
)


def _vertex_creds(env):
    return (
        _param(env, "etp_assessment.vertex_project_id"),
        _param(env, "etp_assessment.vertex_location", "us-central1"),
        _param(env, "etp_assessment.vertex_model", "gemini-3-pro"),
        _param(env, "etp_assessment.vertex_api_key"),
    )


def _minted_bearer(env):
    """Sign a JWT from the uploaded service-account JSON and exchange it for
    a fresh Vertex AI OAuth access token. Caches the token in System
    Parameters until 5 minutes before its declared expiry. Returns '' when
    no service-account JSON is configured.
    """
    import time
    ICP = env["ir.config_parameter"].sudo()
    sa_json = ICP.get_param(
        "etp_assessment.vertex_service_account_json", "") or ""
    if not sa_json or "PLACEHOLDER" in sa_json:
        return ""
    cached = ICP.get_param("etp_assessment.vertex_minted_token", "") or ""
    expires_at = int(
        ICP.get_param("etp_assessment.vertex_minted_token_expires", "0") or 0
    )
    if cached and time.time() < expires_at - 300:
        return cached
    try:
        import jwt as _jwt
        if not hasattr(_jwt, "encode"):
            raise ImportError(
                "Wrong 'jwt' package installed (no 'encode' attr). "
                "Two PyPI packages collide on the name `jwt`; we need PyJWT. "
                "Fix: pip uninstall jwt -y && pip install 'PyJWT[crypto]'")
    except ImportError as exc:
        raise RuntimeError(
            "Service Account JSON support needs PyJWT installed. %s" % exc)
    import httpx
    sa = json.loads(sa_json)
    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": sa["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    assertion = _jwt.encode(claim, sa["private_key"], algorithm="RS256")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            sa["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            "Service account token exchange failed [%s]: %s"
            % (resp.status_code, resp.text[:400]))
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    ICP.set_param("etp_assessment.vertex_minted_token", token)
    ICP.set_param(
        "etp_assessment.vertex_minted_token_expires", str(now + expires_in))
    # SA JSON declares the GCP project; backfill so the aiplatform URL
    # builder doesn't need a separate System Parameter when the user only
    # uploaded the JSON.
    if not ICP.get_param("etp_assessment.vertex_project_id") and \
            sa.get("project_id"):
        ICP.set_param(
            "etp_assessment.vertex_project_id", sa["project_id"])
    _logger.info(
        "etp_assessment minted Vertex bearer for %s (expires in %ss)",
        sa.get("client_email") or "?", expires_in)
    return token


def _vertex_bearer(env):
    """OAuth bearer for the Vertex (aiplatform) endpoint.

    Order: explicit `vertex_access_token` System Parameter wins (manual
    paste, useful for ad-hoc testing). Otherwise fall through to a minted
    bearer from the uploaded service-account JSON.
    """
    direct = _param(env, "etp_assessment.vertex_access_token")
    if direct:
        return direct
    return _minted_bearer(env)


def _gemini_request(env, model, suffix):
    """Return (url, headers) for a Gemini call, auth-aware.

    Three supported auth modes, picked by what is set and (for api_key) by
    key prefix:
      - "AQ." api_key -> Vertex AI Express Mode
            host: aiplatform.googleapis.com (global, no project/location)
            path: /v1/publishers/google/models/{model}:{suffix}
            header: x-goog-api-key
            This is the key bound to your GCP account on Google's side; no
            project_id, no IAM, no token refresh. Same path serves Gemini
            text/multimodal AND Imagen.
      - "AIza" api_key -> Gemini Developer API
            host: generativelanguage.googleapis.com
            path: /v1beta/models/{model}:{suffix}
            header: x-goog-api-key
            Text/multimodal only — does NOT serve Imagen.
      - vertex_access_token (OAuth bearer) -> Vertex AI service-account path
            host: {location}-aiplatform.googleapis.com
            path: /v1/projects/{project}/locations/{location}/publishers/google/models/{model}:{suffix}
            header: Authorization: Bearer
            Requires vertex_project_id. Token expires ~1h.

    Pasting the wrong key shape against the wrong endpoint returns 401/403,
    which is the trap this function avoids.
    """
    project, location, _model, api_key = _vertex_creds(env)
    bearer = _vertex_bearer(env)
    if bearer:
        host = (f"https://{location}-aiplatform.googleapis.com"
                if location != "global" else "https://aiplatform.googleapis.com")
        url = (f"{host}/v1/projects/{project}/locations/{location}"
               f"/publishers/google/models/{model}:{suffix}")
        return url, {"Content-Type": "application/json",
                     "Authorization": f"Bearer {bearer}"}
    if api_key:
        if api_key.startswith("AQ."):
            url = (f"https://aiplatform.googleapis.com/v1/publishers/google/"
                   f"models/{model}:{suffix}")
        else:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:{suffix}")
        return url, {"Content-Type": "application/json",
                     "x-goog-api-key": api_key}
    raise ValueError(
        "Vertex/Gemini not configured. Provide ONE of: "
        "(1) etp_assessment.vertex_api_key (AIza... for Gemini Developer API, "
        "or AQ... for Vertex AI Express Mode); "
        "(2) etp_assessment.vertex_access_token + vertex_project_id "
        "(static OAuth bearer); "
        "(3) upload a service-account JSON in Settings — the module mints "
        "and auto-refreshes bearers from it. "
        "Configure in Settings > ETP Assessment, or directly in "
        "System Parameters.")


def _vertex_endpoint(env):
    """Back-compat shim: (url, api_key) for the text model generateContent."""
    _project, _loc, model, _key = _vertex_creds(env)
    url, headers = _gemini_request(env, model, "generateContent")
    return url, headers


def _call_vertex(env, system_prompt, user_text, max_tokens=4000,
                 temperature=0.4, image_parts=None):
    """Vertex AI Gemini generateContent call. ONE model for everything.

    `image_parts` (optional): list of {"mime_type": ..., "data": <b64 str>}
    — used by multimodal scoring so the model SEES the question images.
    """
    import httpx

    _project, _loc, model, _key = _vertex_creds(env)
    url, headers = _gemini_request(env, model, "generateContent")
    parts = [{"text": user_text}]
    for img in (image_parts or []):
        if img.get("data"):
            parts.append({
                "inlineData": {
                    "mimeType": img.get("mime_type") or "image/png",
                    "data": img["data"],
                }
            })
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }

    _logger.info(
        "etp_assessment Vertex call: model=%s parts=%d (images=%d) max_tokens=%d",
        _vertex_creds(env)[2], len(parts), len(image_parts or []), max_tokens,
    )
    with httpx.Client(
        timeout=httpx.Timeout(connect=30, read=180, write=60, pool=30)
    ) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Vertex error [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Vertex response missing text content: {str(data)[:300]}"
        )


def _default_vertex_model(env):
    return _vertex_creds(env)[2]


def _extract_json_array(text):
    """Pull the first JSON array out of an LLM response (tolerant of stray prose)."""
    text = text.strip()
    # strip markdown code fences
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    raise ValueError("Could not parse JSON array from LLM response: %s" % text[:200])


def extract_skills(env, source_text, system_prompt=None):
    sp = system_prompt or DEFAULT_SKILLS_PROMPT
    raw = _call_vertex(env, sp, source_text, max_tokens=2000, temperature=0.3)
    items = _extract_json_array(raw)
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("skill"):
            out.append({
                "skill": str(it["skill"])[:120],
                "reason": str(it.get("reason", ""))[:300],
                "suggested_max": int(it.get("suggested_max", 5) or 5),
            })
    return out


def generate_questions_from_seed(env, sop_text, golden_example,
                                 system_prompt=None, max_questions=0,
                                 max_tokens=8000):
    """SEED FLOW (research-team design): ONE call, no skills stage.

    Input = SOP + one golden example question. Output = flat question list,
    same item shape as generate_questions (incl. image_prompt_a/b).
    max_questions=0 means let the model decide.
    """
    sp = system_prompt or DEFAULT_SEED_PROMPT
    count_line = (
        f"Generate exactly {max_questions} questions.\n\n"
        if max_questions else ""
    )
    user = (
        f"PROJECT SOP:\n{sop_text}\n\n"
        f"GOLDEN EXAMPLE QUESTION (match this style, depth and format):\n"
        f"{golden_example}\n\n"
        f"{count_line}"
        f"Generate the questions now as one JSON array."
    )
    raw = _call_vertex(env, sp, user, max_tokens=max_tokens, temperature=0.5)
    items = _extract_json_array(raw)
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        if max_questions and len(out) >= max_questions:
            break
        out.append({
            "skill": str(it.get("skill", "")).strip(),  # optional, kept if model emits it
            "title": str(it["title"])[:200],
            "prompt": str(it.get("prompt", it["title"])),
            "type": it.get("type", "text") if it.get("type") in
                    ("text", "coding", "image_comparison", "image_text", "video")
                    else "text",
            "image_prompt_a": str(it.get("image_prompt_a", ""))[:1024],
            "image_prompt_b": str(it.get("image_prompt_b", ""))[:1024],
        })
    return out


def generate_questions(env, source_text, skills, system_prompt=None, max_tokens=8000):
    """Generate questions for ALL skills in ONE Vertex AI Gemini call (minimize calls).

    `skills` is a list of {"name": str, "max_questions": int}.
    Returns a flat list of {"skill","title","prompt","type"}.
    """
    sp = system_prompt or DEFAULT_QUESTIONS_PROMPT
    skill_lines = "\n".join(
        f"- {s['name']}: {s['max_questions']} questions" for s in skills
    )
    caps = {s["name"]: s["max_questions"] for s in skills}
    user = (
        f"PROJECT TEXT:\n{source_text}\n\n"
        f"SKILLS AND HOW MANY QUESTIONS EACH:\n{skill_lines}\n\n"
        f"Generate all questions now, grouped by skill, as one JSON array."
    )
    raw = _call_vertex(env, sp, user, max_tokens=max_tokens, temperature=0.5)
    items = _extract_json_array(raw)
    out = []
    per_skill = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        sk = str(it.get("skill", "")).strip() or (skills[0]["name"] if skills else "")
        # respect per-skill cap
        n = per_skill.get(sk, 0)
        if sk in caps and n >= caps[sk]:
            continue
        per_skill[sk] = n + 1
        out.append({
            "skill": sk,
            "title": str(it["title"])[:200],
            "prompt": str(it.get("prompt", it["title"])),
            "type": it.get("type", "text") if it.get("type") in
                    ("text", "coding", "image_comparison", "image_text", "video")
                    else "text",
            # image_comparison questions carry the two text-to-image prompts;
            # vertex_images.py turns them into actual images on demand.
            "image_prompt_a": str(it.get("image_prompt_a", ""))[:1024],
            "image_prompt_b": str(it.get("image_prompt_b", ""))[:1024],
        })
    return out
