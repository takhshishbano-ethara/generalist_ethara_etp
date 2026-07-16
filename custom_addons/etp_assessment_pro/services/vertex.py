import json
import logging
import os
import re
import time

from ..constants import (
    QUESTION_TYPE_CODES as _QUESTION_TYPES,
    DIFFICULTY_CODES as _DIFFICULTIES,
    IMAGE_QUESTION_TYPES as _IMAGE_TYPES,
    VIDEO_QUESTION_TYPES as _VIDEO_TYPES,
    MEDIUM_CODES as _MEDIA,
    AB_DIMENSION_NAMES as _AB_DIM_NAMES,
    AB_CHOICES as _AB_CHOICES,
    AB_CHOICE_SET as _AB_CHOICE_SET,
    AB_FLAWED_SIDES,
    ab_side_verdict, ab_other_side,
    ab_flip_construction_keys, ab_specs_from_construction_keys,
    validate_flaw_plan, normalize_flaw_plan,
    QUESTION_TYPE_PROMPT_LIST,
    QUESTION_TYPE_ORDER as _QUESTION_TYPE_ORDER,
    VERTEX_DEFAULT_LOCATION,
    VERTEX_DEFAULT_MODEL,
    GENERATION_DEFAULT_MODEL,
    VERTEX_GLOBAL_LOCATION,
    VIDEO_DEFAULT_MODEL,
    VIDEO_DEFAULT_LOCATION,
    VIDEO_DEFAULT_DURATION_S,
    ADVISORY_LOCK_VERTEX_BEARER,
    text_has_source_reference,
)

_logger = logging.getLogger(__name__)

# video_prompt is the video twin of image_prompt: it authors an image_specs
# draft (briefs + answer key) rather than options, so it routes through the same
# _build_image_draft_fields / forced-directive gate as the image types.
_IMAGE_OR_VIDEO_TYPES = frozenset(_IMAGE_TYPES | _VIDEO_TYPES)

INLINE_QUESTION_PROMPT = (
    "You are an expert assessment author. Generate questions for the given "
    "SKILL grounded in the supplied artifacts. Return ONLY a JSON array, no "
    "markdown. Each item: name (short title), prompt (full question text), "
    'question_type (one of ' + QUESTION_TYPE_PROMPT_LIST + '), '
    'difficulty (easy/medium/hard), and the answer-key '
    "fields its type needs: mcq -> options (list) + correct_answer (string); "
    "msq -> options (list) + correct_answer (list); subjective_rubric -> "
    "rubric (object with checklist/constraints/pass_condition). For the "
    "image types the per-request directive gives the exact image_specs shape; "
    "do NOT emit options/correct_answer for image types. Every question MUST be "
    "self-contained: never reference the SOP, source, or guidelines — bake the "
    "deciding facts into the scenario itself."
)

_SELF_CONTAINED_RULE = (
    "\n\nHARD RULE — SELF-CONTAINED: Every question MUST be answerable from the "
    "scenario you write plus the candidate's skill ALONE. NEVER reference, cite, "
    "quote, or allude to the SOP, the source material, the guidelines, a "
    "Section/Step/Clause, or any document the candidate cannot see. Bake every "
    "deciding fact into the scenario. This binds the prompt, options, rubric and "
    "official_reasoning."
)

_GEN_SELF_CONTAINED_MAX_ATTEMPTS = 3
_SOURCE_LEAK_CORRECTION = (
    "\n\nCORRECTION — your previous attempt is REJECTED: one or more questions "
    "referenced the source the candidate cannot see (e.g. \"According to the "
    "SOP\", \"the SOP states\", \"the project workflow\", \"Section/Step N\"). "
    "Regenerate ALL questions fully self-contained: make the rule itself the task "
    "and state every deciding fact inside the scenario. Do NOT mention the SOP, "
    "the source, the guidelines, the project workflow, or any Section/Step/Clause "
    "anywhere in any field."
)


def _item_cites_source(item):
    """True when a generated question item references the source the candidate
    never sees — checked across name, prompt, options, correct_answer, rubric,
    official_reasoning and image_specs. Drives the regenerate-on-leak guard."""
    if not isinstance(item, dict):
        return False
    parts = [item.get("name"), item.get("prompt"), item.get("official_reasoning")]
    opts = item.get("options")
    if isinstance(opts, list):
        parts.extend(str(o) for o in opts)
    ca = item.get("correct_answer")
    parts.extend(str(c) for c in ca) if isinstance(ca, list) else parts.append(
        str(ca) if ca else None)
    for key in ("rubric", "image_specs"):
        val = item.get(key)
        if isinstance(val, dict):
            parts.append(json.dumps(val, ensure_ascii=False))
    return text_has_source_reference(*[p for p in parts if p])


def _param(env, key, default=""):
    val = env["ir.config_parameter"].sudo().get_param(key, default) or default
    if isinstance(val, str) and "PLACEHOLDER" in val:
        return default
    return val


def _vertex_creds(env):
    return (
        _param(env, "etp_assessment_pro.vertex_project_id"),
        _param(env, "etp_assessment_pro.vertex_location", VERTEX_DEFAULT_LOCATION),
        _param(env, "etp_assessment_pro.vertex_model", VERTEX_DEFAULT_MODEL),
        _param(env, "etp_assessment_pro.vertex_api_key"),
    )


def _vertex_image_model(env):
    """The model used to RENDER images. Prefers a dedicated ``image_model``
    config key so the bare ``vertex_model`` default can be a cheap TEXT model
    without ever routing image rendering onto it (and vice-versa). Falls back to
    ``vertex_model`` for backward compatibility with deployments that set only
    the single Default Model field."""
    return _param(env, "etp_assessment_pro.image_model") \
        or _vertex_creds(env)[2]


_HTTPX_CLIENT = None


def _httpx():
    """SVC-5: one pooled, thread-safe httpx client reused across Vertex calls
    instead of building and tearing one down per request."""
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None:
        import httpx
        _HTTPX_CLIENT = httpx.Client(
            timeout=httpx.Timeout(connect=30, read=180, write=60, pool=30),
            limits=httpx.Limits(max_keepalive_connections=20,
                                max_connections=50))
    return _HTTPX_CLIENT


def _minted_bearer(env):
    ICP = env["ir.config_parameter"].sudo()
    sa_json = ICP.get_param(
        "etp_assessment_pro.vertex_service_account_json", ""
    ) or ""
    if not sa_json or "PLACEHOLDER" in sa_json:
        return ""
    cached = ICP.get_param("etp_assessment_pro.vertex_minted_token", "") or ""
    expires_at = int(
        ICP.get_param("etp_assessment_pro.vertex_minted_token_expires", "0") or 0
    )
    if cached and time.time() < expires_at - 300:
        return cached
    env.cr.execute(
        "SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_VERTEX_BEARER,))
    cached = ICP.get_param("etp_assessment_pro.vertex_minted_token", "") or ""
    expires_at = int(
        ICP.get_param("etp_assessment_pro.vertex_minted_token_expires", "0") or 0)
    if cached and time.time() < expires_at - 300:
        return cached
    try:
        import jwt as _jwt
        if not hasattr(_jwt, "encode"):
            raise ImportError(
                "Wrong 'jwt' package installed (no 'encode' attr). "
                "Two PyPI packages collide on the name `jwt`; we need PyJWT. "
                "Fix: pip uninstall jwt -y && pip install 'PyJWT[crypto]'"
            )
    except ImportError as exc:
        raise RuntimeError(
            "Service Account JSON support needs PyJWT installed. %s" % exc
        )
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
    resp = _httpx().post(
        sa["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            "Service account token exchange failed [%s]: %s"
            % (resp.status_code, resp.text[:400])
        )
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    ICP.set_param("etp_assessment_pro.vertex_minted_token", token)
    ICP.set_param(
        "etp_assessment_pro.vertex_minted_token_expires", str(now + expires_in)
    )
    if not ICP.get_param("etp_assessment_pro.vertex_project_id") and sa.get("project_id"):
        ICP.set_param("etp_assessment_pro.vertex_project_id", sa["project_id"])
    _logger.info(
        "etp_assessment minted Vertex bearer for %s (expires in %ss)",
        sa.get("client_email") or "?", expires_in,
    )
    return token


def _vertex_bearer(env):
    direct = _param(env, "etp_assessment_pro.vertex_access_token")
    if direct:
        return direct
    return _minted_bearer(env)


def _gemini_request(env, model, suffix):
    project, location, _model, api_key = _vertex_creds(env)
    bearer = _vertex_bearer(env)
    if bearer:
        host = (
            f"https://{location}-aiplatform.googleapis.com"
            if location and location != VERTEX_GLOBAL_LOCATION
            else "https://aiplatform.googleapis.com"
        )
        url = (
            f"{host}/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:{suffix}"
        )
        return url, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
        }
    if api_key:
        if api_key.startswith("AQ."):
            url = (
                f"https://aiplatform.googleapis.com/v1/publishers/google/"
                f"models/{model}:{suffix}"
            )
        else:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:{suffix}"
            )
        return url, {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
    raise ValueError(
        "Vertex/Gemini not configured. Provide ONE of: "
        "(1) etp_assessment_pro.vertex_api_key (AIza... or AQ...); "
        "(2) etp_assessment_pro.vertex_access_token + vertex_project_id; "
        "(3) upload a service-account JSON in Settings."
    )


def _load_bundled_prompt(filename):
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
            filename, exc,
        )
        return ""


def _get_question_prompt(env):
    p = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment_pro.question_prompt", "") or "").strip()
    if p:
        return p
    bundled = _load_bundled_prompt("question.md")
    return bundled.strip() if bundled.strip() else INLINE_QUESTION_PROMPT


_PRICING = {
    "gemini-3.1-pro-preview": {"in": 2.00, "out": 12.00, "image": 0.0},
    "gemini-3-pro-image":     {"in": 2.00, "out": 12.00, "image": 0.134},
    "gemini-2.5-flash-image": {"in": 0.30, "out": 2.50,  "image": 0.039},
    "gemini-2.5-flash":       {"in": 0.30, "out": 2.50,  "image": 0.0},
    "gemini-2.5-flash-lite":  {"in": 0.10, "out": 0.40,  "image": 0.0},
    "gemini-2.5-pro":         {"in": 1.25, "out": 10.00, "image": 0.0},
}
_DEFAULT_PRICE = {"in": 1.0, "out": 5.0, "image": 0.0}

# --- VIDEO (Veo) pricing: billed PER SECOND of generated clip, NOT per token.
# Kept separate from _PRICING so the per-1k-token math above is never touched.
# Public list rates (USD per second of output); audio-on Veo 3.x ~ $0.40/s,
# the 'fast' variants ~ $0.15/s, Veo 2 ~ $0.35/s. Each Veo submit = one clip.
_VIDEO_PRICING = {
    "veo-3.1-generate-001":       0.40,
    "veo-3.1-fast-generate-001":  0.15,
    "veo-3.0-generate-001":       0.40,
    "veo-3.0-fast-generate-001":  0.15,
    "veo-2.0-generate-001":       0.35,
}
_DEFAULT_VIDEO_RATE = 0.40


def _estimate_cost(model, tokens_in, tokens_out, thoughts, image_count):
    p = _PRICING.get(model or "", _DEFAULT_PRICE)
    out_tok = (tokens_out or 0) + (thoughts or 0)
    return (((tokens_in or 0) * p["in"] + out_tok * p["out"]) / 1_000_000.0
            + (image_count or 0) * p["image"])


def _estimate_video_cost(model, video_seconds):
    """Per-second Veo cost for one generated clip. Independent of token math."""
    rate = _VIDEO_PRICING.get(model or "", _DEFAULT_VIDEO_RATE)
    return (video_seconds or 0.0) * rate


def _log_usage(env, model, usage_meta, image_count, ctx):
    """Write one LLM-usage ledger row. Best-effort: failures are swallowed."""
    try:
        meta = usage_meta or {}
        ctx = ctx or {}
        ti = int(meta.get("promptTokenCount") or 0)
        to = int(meta.get("candidatesTokenCount") or 0)
        th = int(meta.get("thoughtsTokenCount") or 0)
        vsec = float(ctx.get("video_seconds") or 0.0)
        cost = (_estimate_video_cost(model, vsec) if vsec > 0
                else _estimate_cost(model, ti, to, th, image_count))
        env["etp.assessment.pro.llm.usage"].sudo().create({
            "operation": ctx.get("operation") or "other",
            "model": model or "",
            "tokens_in": ti,
            "tokens_out": to,
            "thoughts_tokens": th,
            "image_count": image_count or 0,
            "video_seconds": vsec,
            "cost_usd": cost,
            "prompt_id": ctx.get("prompt_id") or False,
            "evaluator_id": ctx.get("evaluator_id") or False,
            "note": (ctx.get("note") or "")[:120],
        })
        _logger.info(
            "etp_assessment LLM usage: op=%s model=%s in=%d out=%d thoughts=%d "
            "images=%d vsec=%.1f cost=$%.4f note=%s",
            ctx.get("operation") or "other", model or "", ti, to, th,
            image_count or 0, vsec, cost,
            (ctx.get("note") or "")[:60])
    except Exception:
        _logger.exception("etp_assessment: LLM usage log failed")


class LLMRefusalError(RuntimeError):
    """The model declined / was blocked / returned no usable text.

    Distinct from transport/parse errors so callers can give an actionable message.
    """


_BLOCKING_FINISH_REASONS = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "OTHER",
}
_REFUSAL_MARKERS = (
    "i cannot fulfill", "i can't fulfill", "i cannot create",
    "i can't create", "i cannot generate", "i can't generate",
    "i'm unable to", "i am unable to", "my current capab",
    "as an ai", "i cannot provide", "i can't provide",
)


def _detect_refusal_text(text):
    """Reason string if ``text`` reads like a prose refusal (not JSON), else None."""
    t = (text or "").strip()
    if not t:
        return "the model returned an empty response"
    head = t[:400].lower()
    if t[:1] in ("[", "{"):
        return None
    for marker in _REFUSAL_MARKERS:
        if marker in head:
            return "the model declined: %s" % t[:200]
    return None


_NO_THINKING_BUDGET_ZERO = ("gemini-2.5-pro",)


def _apply_thinking_budget(gen_config, model):
    """Ask the model to skip 'thinking' so hidden tokens don't truncate the JSON.
    Skipped for Gemini 2.5 Pro (rejects budget 0, HTTP 400). gemini-3-pro-image
    accepts the flag (it just ignores it and thinks anyway — its thought parts
    are filtered out when reading the response)."""
    m = (model or "").strip()
    if any(m.startswith(x) for x in _NO_THINKING_BUDGET_ZERO):
        return
    gen_config["thinkingConfig"] = {"thinkingBudget": 0}


_MAX_OUTPUT_TOKENS_CEILING = 64000

# Start generation at the output ceiling: a higher cap costs nothing extra for
# short outputs (billed per generated token, stops at finish=STOP) but avoids a
# doomed truncate + unparseable-JSON double call on large dense image_label JSON.
_GEN_MAX_OUTPUT_TOKENS = 64000

_TERSE_RETRY_DIRECTIVE = (
    "CRITICAL: Output ONLY the JSON array — no reasoning, no explanation, no "
    "markdown fences. Keep every string field concise (short prompts, no repeated "
    "boilerplate) so the ENTIRE JSON array fits in a single response and is never "
    "truncated."
)


def _json_parses(text):
    """True if ``text`` (optionally markdown-fenced) is parseable JSON."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        json.loads(t)
        return True
    except Exception:
        return False


def _call_vertex(env, system_prompt, user_text, max_tokens=4000,
                 temperature=0.4, usage_ctx=None, response_json=False,
                 response_schema=None, user_parts=None, model=None):
    """Single generateContent call with a built-in MAX_TOKENS recovery.

    Heavy hidden 'thinking' can eat the whole budget (finishReason=MAX_TOKENS,
    no parts); we retry once with a doubled budget (<= _MAX_OUTPUT_TOKENS_CEILING).
    ``user_parts`` (a list of Gemini content parts) supersedes ``user_text`` for
    multimodal calls (e.g. a native SOP document); ``model`` overrides the
    default configured model.
    """
    import httpx
    _project, _loc, default_model, _key = _vertex_creds(env)
    model = model or default_model
    url, headers = _gemini_request(env, model, "generateContent")

    attempt_tokens = max_tokens
    last_finish = None
    for attempt in range(2):
        gen_config = {
            "maxOutputTokens": attempt_tokens,
            "temperature": temperature,
        }
        _apply_thinking_budget(gen_config, model)
        if response_json:
            gen_config["responseMimeType"] = "application/json"
            if response_schema:
                gen_config["responseSchema"] = response_schema
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user",
                          "parts": user_parts or [{"text": user_text}]}],
            "generationConfig": gen_config,
        }
        _logger.info(
            "etp_assessment Vertex call: model=%s max_tokens=%d json=%s attempt=%d",
            model, attempt_tokens, response_json, attempt + 1,
        )
        resp = _httpx().post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            if resp.status_code == 429:
                raise VertexQuotaError(
                    f"Vertex quota exhausted [429]: {resp.text[:200]}")
            raise RuntimeError(
                f"Vertex error [{resp.status_code}]: {resp.text[:400]}"
            )
        data = resp.json()
        _log_usage(env, model, data.get("usageMetadata"), 0, usage_ctx)
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMRefusalError(
                "the model blocked this request (%s). This usually means the "
                "question/skill does not fit the requested medium." % block)
        try:
            cand = data["candidates"][0]
        except (KeyError, IndexError, TypeError):
            raise LLMRefusalError(
                "the model returned no candidates: %s" % str(data)[:200])
        finish = cand.get("finishReason")
        last_finish = finish
        if finish in _BLOCKING_FINISH_REASONS:
            raise LLMRefusalError(
                "the model stopped without an answer (reason: %s). This usually "
                "means a safety block or a medium mismatch." % finish)
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(
            p["text"] for p in parts
            if isinstance(p, dict) and p.get("text") and not p.get("thought"))
        if not text:
            if (finish == "MAX_TOKENS" and attempt == 0
                    and attempt_tokens < _MAX_OUTPUT_TOKENS_CEILING):
                attempt_tokens = min(attempt_tokens * 2,
                                     _MAX_OUTPUT_TOKENS_CEILING)
                _logger.warning(
                    "etp_assessment Vertex MAX_TOKENS with no text; retrying "
                    "with maxOutputTokens=%d", attempt_tokens)
                continue
            raise LLMRefusalError(
                "the model returned no text content (finishReason: %s)" % finish)
        if (finish == "MAX_TOKENS" and attempt == 0
                and attempt_tokens < _MAX_OUTPUT_TOKENS_CEILING):
            attempt_tokens = min(attempt_tokens * 2, _MAX_OUTPUT_TOKENS_CEILING)
            _logger.warning(
                "etp_assessment Vertex MAX_TOKENS with truncated text (%d chars); "
                "retrying with maxOutputTokens=%d", len(text), attempt_tokens)
            continue
        refusal = _detect_refusal_text(text)
        if refusal:
            raise LLMRefusalError(refusal)
        if (response_json and attempt == 0
                and attempt_tokens < _MAX_OUTPUT_TOKENS_CEILING
                and not _json_parses(text)):
            attempt_tokens = min(attempt_tokens * 2, _MAX_OUTPUT_TOKENS_CEILING)
            _logger.warning(
                "etp_assessment Vertex returned unparseable JSON (%d chars, "
                "finish=%s); retrying with maxOutputTokens=%d",
                len(text), finish, attempt_tokens)
            continue
        _logger.info(
            "etp_assessment Vertex result: model=%s finish=%s chars=%d attempt=%d",
            model, finish, len(text), attempt + 1)
        return text

    raise LLMRefusalError(
        "the model returned no text content after a token-budget retry "
        "(finishReason: %s)" % last_finish)


class VertexQuotaError(RuntimeError):
    """Vertex returned HTTP 429 (quota / rate limit). Transient: the caller
    re-queues and retries later instead of marking the item permanently failed.

    ``partial`` carries any already-rendered slots (list of image dicts) that a
    multi-image render managed to produce before the 429 fired, so the caller
    can persist them and re-render only the missing slots — never re-paying for
    a picture that already came back. Empty for single-image / non-render calls.
    """

    partial = []


def generate_image(env, image_prompt, *, aspect_hint=None, usage_ctx=None):
    """Text->image via the Gemini image model. Returns ``(b64_str, mime)``;
    raises VertexQuotaError on 429, RuntimeError when no image part comes back."""
    import httpx
    model = _vertex_image_model(env)
    url, headers = _gemini_request(env, model, "generateContent")
    prompt_text = image_prompt or ""
    if aspect_hint:
        prompt_text = f"{prompt_text}\n\n(Aspect/framing hint: {aspect_hint})"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "temperature": 1.0,
        },
    }
    _logger.info("etp_assessment Vertex image gen: model=%s", model)
    resp = _httpx().post(url, json=payload, headers=headers)
    if resp.status_code == 429:
        raise VertexQuotaError(
            f"Vertex image quota exhausted [429]: {resp.text[:200]}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"Vertex image error [{resp.status_code}]: {resp.text[:400]}"
        )
    data = resp.json()
    _log_usage(env, model, data.get("usageMetadata"), 1, usage_ctx)
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Vertex image response missing candidates: {str(data)[:300]}"
        )
    for part in parts or []:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = (inline.get("mimeType") or inline.get("mime_type")
                    or "image/png")
            _logger.info(
                "etp_assessment Vertex image ok: model=%s mime=%s b64_chars=%d",
                model, mime, len(inline["data"] or ""))
            return inline["data"], mime
    raise RuntimeError(
        "Vertex image response had no image part (safety block / refusal?): "
        f"{str(data)[:300]}"
    )


def _video_model(env):
    return _param(env, "etp_assessment_pro.video_model", VIDEO_DEFAULT_MODEL) \
        or VIDEO_DEFAULT_MODEL


def _video_location(env):
    return _param(
        env, "etp_assessment_pro.video_location", VIDEO_DEFAULT_LOCATION) \
        or VIDEO_DEFAULT_LOCATION


def _video_default_duration(env):
    raw = _param(env, "etp_assessment_pro.video_default_duration_s", "")
    try:
        return int(float(raw)) if str(raw).strip() else VIDEO_DEFAULT_DURATION_S
    except (TypeError, ValueError):
        return VIDEO_DEFAULT_DURATION_S


def video_generation_available(env):
    """Config gate for async Veo generation: True only when a Veo model AND a
    resolvable Vertex bearer are present. When it is False the submit trigger
    leaves a video_prompt draft 'pending' so the admin fills its clips by
    upload (Phase 1) — video generation is strictly optional."""
    if not _video_model(env):
        return False
    try:
        return bool(_vertex_bearer(env))
    except Exception:  # noqa: BLE001 - a creds error means "not available"
        return False


def _veo_url(env, model, location, suffix):
    project = _vertex_creds(env)[0] \
        or _param(env, "etp_assessment_pro.vertex_project_id")
    host = f"https://{location}-aiplatform.googleapis.com"
    return (f"{host}/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:{suffix}")


def _brief_prompt_text(brief):
    if isinstance(brief, str):
        return brief
    if isinstance(brief, dict):
        return brief.get("prompt") or ""
    return ""


def submit_video_op(env, brief, *, model=None, location=None, duration_s=None,
                    aspect="16:9", prompt_id=None):
    """Submit ONE Veo long-running text-to-video op and return its operation
    NAME (the poll handle). ``brief`` is the clip brief (a dict with a "prompt",
    or the prompt string). Model/location resolve from the video_model /
    video_location params (defaults veo-3.1-generate-001 / us-central1 — Veo
    404s on 'global', so never reuse the gemini location). Raises
    VertexQuotaError on 429 so the caller re-queues without failing."""
    model = model or _video_model(env)
    location = location or _video_location(env)
    bearer = _minted_bearer(env) or _vertex_bearer(env)
    if not bearer:
        raise ValueError(
            "Veo video generation needs a Vertex OAuth bearer (service-account "
            "JSON or a static access token). Configure it or leave video_prompt "
            "clips upload-only.")
    prompt_text = _brief_prompt_text(brief)
    if duration_s is None:
        duration_s = (brief.get("duration_s") or brief.get("durationSeconds")) \
            if isinstance(brief, dict) else None
        duration_s = int(duration_s) if duration_s else _video_default_duration(env)
    url = _veo_url(env, model, location, "predictLongRunning")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {bearer}"}
    payload = {
        "instances": [{"prompt": prompt_text}],
        "parameters": {
            "durationSeconds": int(duration_s),
            "generateAudio": True,
            "sampleCount": 1,
            "aspectRatio": aspect or "16:9",
        },
    }
    _logger.info("etp_assessment Veo submit: model=%s loc=%s dur=%ss",
                 model, location, duration_s)
    resp = _httpx().post(url, json=payload, headers=headers)
    if resp.status_code == 429:
        raise VertexQuotaError(
            f"Veo submit quota exhausted [429]: {resp.text[:200]}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"Veo submit error [{resp.status_code}]: {resp.text[:400]}")
    op_name = (resp.json() or {}).get("name")
    if not op_name:
        raise RuntimeError(
            f"Veo submit returned no operation name: {resp.text[:300]}")
    _log_usage(env, model, None, 0,
               {"operation": "submit_video_op",
                "video_seconds": int(duration_s),
                "prompt_id": prompt_id or False,
                "note": (prompt_text or "")[:80]})
    return op_name


def _extract_video_payload(resp):
    """Pull ``(video_b64, gcs_uri)`` out of a done Veo operation response,
    walking the several shapes Veo returns (videos / generatedSamples /
    predictions, each carrying inline base64 bytes and/or a GCS uri)."""
    if not isinstance(resp, dict):
        return None, None
    items = []
    for key in ("videos", "generatedSamples", "generated_videos",
                "predictions"):
        val = resp.get(key)
        if isinstance(val, list):
            items.extend(val)
    for item in items:
        if not isinstance(item, dict):
            continue
        b64 = (item.get("bytesBase64Encoded") or item.get("videoBytes")
               or item.get("b64_json"))
        gcs = item.get("gcsUri") or item.get("uri") or item.get("gcs_uri")
        video = item.get("video")
        if isinstance(video, dict):
            b64 = b64 or video.get("bytesBase64Encoded") \
                or video.get("videoBytes")
            gcs = gcs or video.get("gcsUri") or video.get("uri") \
                or video.get("gcs_uri")
        if b64 or gcs:
            return b64, gcs
    return None, None


def fetch_video_op(env, op_name, *, model, location):
    """Poll ONE Veo op, returning ``{done, video_b64, gcs_uri, error}`` parsed
    defensively. Raises VertexQuotaError on 429 (transient: the caller keeps the
    op and retries next tick)."""
    model = model or _video_model(env)
    location = location or _video_location(env)
    bearer = _minted_bearer(env) or _vertex_bearer(env)
    if not bearer:
        raise ValueError("Veo poll needs a Vertex OAuth bearer.")
    url = _veo_url(env, model, location, "fetchPredictOperation")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {bearer}"}
    resp = _httpx().post(url, json={"operationName": op_name}, headers=headers)
    if resp.status_code == 429:
        raise VertexQuotaError(
            f"Veo poll quota exhausted [429]: {resp.text[:200]}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"Veo poll error [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json() if resp.content else {}
    out = {"done": False, "video_b64": None, "gcs_uri": None, "error": None}
    if not isinstance(data, dict):
        return out
    out["done"] = bool(data.get("done"))
    err = data.get("error")
    if isinstance(err, dict) and err:
        out["error"] = str(err.get("message") or err)[:300]
    b64, gcs = _extract_video_payload(data.get("response") or {})
    out["video_b64"] = b64
    out["gcs_uri"] = gcs
    return out


def _unwrap_json_list(value):
    """Return the list of items from an LLM JSON response.

    Accepts a bare top-level array, OR an object that wraps the array under a
    common key (skills / items / questions / data / results). A prompt that
    returns ``{"skills": [...]}`` (or the governance-style object) then parses
    the same as a bare ``[...]`` — iterating a dict would otherwise yield its
    KEYS and silently produce zero items.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("skills", "items", "questions", "data", "results", "result"):
            if isinstance(value.get(key), list):
                return value[key]
    return value


def _salvage_json_objects(text):
    """Recover the leading COMPLETE objects of a truncated JSON array.

    When the model hits the output-token ceiling mid-array the response ends with
    a partial trailing object, and neither ``json.loads`` nor the greedy ``[.*]``
    regex can parse it. Locate the first ``[`` then repeatedly ``raw_decode``
    top-level objects, skipping the commas/whitespace between them, until the
    truncation point; return every fully-parsed object and drop the incomplete
    tail. Returns ``[]`` when nothing complete can be recovered."""
    start = text.find("[")
    if start == -1:
        return []
    decoder = json.JSONDecoder()
    i = start + 1
    n = len(text)
    out = []
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        out.append(obj)
    return out


def _extract_json_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return _unwrap_json_list(json.loads(text))
    except Exception:
        pass
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                out = _unwrap_json_list(json.loads(m.group(0)))
                if isinstance(out, list):
                    return out
            except Exception:
                pass
    salvaged = _salvage_json_objects(text)
    if salvaged:
        _logger.warning(
            "etp_assessment recovered %d complete item(s) from a truncated JSON "
            "response (dropped the trailing partial item).", len(salvaged))
        return salvaged
    raise ValueError(
        "Could not parse JSON array from LLM response: %s" % text[:200]
    )


_DETECT_SYSTEM_PROMPT = (
    "You are a precise visual object detector. Return ONLY the JSON array the "
    "user requests, matching the response schema, with no markdown or prose."
)

_DETECT_PROMPT = (
    "Identify DISTINCT TYPES of objects in this image for a visual quiz. "
    "Choose the single clearest example of each different object category and give "
    "it its own tight 2D bounding box, a short lowercase label (1-2 words, e.g. "
    "'car', 'bus', 'dog', 'tree', 'building', 'person', 'sign'), and a one-line "
    "description. Each label MUST be a different category: never repeat the same "
    "label and never return two boxes of the same object type. Maximize the VARIETY "
    "of categories present (vehicles, animals, people, plants, buildings, signs, "
    "everyday items, etc.). Never box the whole scene, sky, road, or background; "
    "each box must be tight around exactly one object. Order top-to-bottom, "
    "left-to-right. Return 3-15 boxes, each a UNIQUE object type."
)

_UI_PROMPT = (
    "This is a screenshot of a website or app interface. Detect the INTERACTIVE, "
    "clickable elements a user could act on: buttons, links, navigation menu items, "
    "tabs, icons (search, cart, menu, profile, back), input and search fields, "
    "logos, and call-to-action elements. For each, return a tight 2D bounding box, "
    "a short label naming the element and its action (e.g. 'search button', "
    "'cart icon', 'sign in', 'menu', 'add to cart', 'home link'), and a one-line "
    "description of what it does. Number EVERY distinct clickable element separately, "
    "even if several are the same type (e.g. multiple 'buy' buttons). Do not box "
    "large non-interactive regions, background images, or paragraphs of body text. "
    "Order top-to-bottom, left-to-right. Return the 5-40 clearest interactive elements."
)

BOX_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "box_2d": {
                "type": "ARRAY",
                "items": {"type": "INTEGER"},
                "minItems": 4,
                "maxItems": 4,
            },
            "label": {"type": "STRING"},
            "description": {"type": "STRING"},
        },
        "required": ["box_2d", "label"],
    },
}


def _detection_model(env):
    return _param(env, "etp_assessment_pro.detection_model") \
        or _generation_model(env)


def detect_image_elements(env, image_b64, ui=False, model=None, usage_ctx=None):
    """Detect distinct objects (or interactive UI elements when ``ui``) in the
    base64 PNG, returning ``[{"box_2d":[ymin,xmin,ymax,xmax], "label", "description"}]``
    with boxes normalized to Gemini's 0-1000 space. De-duplicates object
    categories (unique lowercase label) for non-ui, keeps every element for ui,
    mirroring the reference prototype.

    ``usage_ctx`` lets the caller thread the owning generator (``prompt_id``) onto
    the logged usage row so this detection spend attributes as that project's
    authoring cost instead of landing in the dashboard's Unattributed bucket; the
    operation is always forced to ``detect_image_elements``."""
    model = model or _detection_model(env)
    prompt = _UI_PROMPT if ui else _DETECT_PROMPT
    user_parts = [
        {"inlineData": {"mimeType": "image/png", "data": image_b64}},
        {"text": prompt},
    ]
    ctx = dict(usage_ctx or {})
    ctx["operation"] = "detect_image_elements"
    ctx.setdefault("note", "ui" if ui else "objects")
    raw = _call_vertex(
        env, _DETECT_SYSTEM_PROMPT, user_text="", user_parts=user_parts,
        model=model, max_tokens=8000, temperature=0.0, response_json=True,
        response_schema=BOX_SCHEMA, usage_ctx=ctx)
    detections = []
    for it in _extract_json_array(raw):
        if not isinstance(it, dict):
            continue
        box = it.get("box_2d")
        if not isinstance(box, list) or len(box) != 4:
            continue
        try:
            coords = [int(round(float(c))) for c in box]
        except (TypeError, ValueError):
            continue
        detections.append({
            "box_2d": coords,
            "label": str(it.get("label") or "").strip(),
            "description": str(it.get("description") or "").strip(),
        })
    if ui:
        return detections
    seen = set()
    unique = []
    for det in detections:
        key = det["label"].lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(det)
    return unique


_IMG_PROMPT_RULE = (
    "Each image prompt MUST be a DETAILED, self-contained scene brief that "
    "states EVERY visually deciding detail (subjects, layout/composition, "
    "colours, materials, lighting) and QUOTES verbatim any text/labels/numbers "
    "that must appear in the image so it renders legibly. Default to a "
    "photorealistic style unless the scenario requires otherwise. Write it as a "
    "single source of truth, not a vague caption."
)


def _ab_fallback_dims():
    """The built-in A/B rubric as [{"label","choices"}], from constants only."""
    return [{"label": name, "choices": list(_AB_CHOICES)}
            for name in _AB_DIM_NAMES.values()]


def _image_type_contract(qtype, ab_dims=None):
    """The per-type ``image_specs`` OUTPUT CONTRACT body only (no "generate N"
    preamble), so it can be appended BOTH to the exclusive forced-type directive
    AND to the generic multi-type SOP directive without over-pinning the run to a
    single type. For image_ab this is the flaw_plan/construction_keys contract the
    platform materializes into the answer key (Phase 3)."""
    if qtype == "image_ab":
        return (
            "FLAW-INJECTION (mandatory): from a single TARGET brief, plan a PAIR "
            "of images and plant deliberate, VISIBLE flaws so the answer key is "
            "ground-truth BY CONSTRUCTION. Output image_specs = {\"flaw_plan\": {"
            '"faithful_side": "a" | "b" | null, "worker_prompt": "the TRUE target '
            'brief shown to the candidate (what a correct image should depict)", '
            '"render_prompts": {"a": "a COMPLETE self-contained brief for image '
            'A", "b": "a COMPLETE self-contained brief for image B"}, "planted": '
            '{"a": ["visible flaw on A", ...], "b": ["visible flaw on B", ...]}, '
            '"construction_keys": {"IF": "<verdict>", "VQ": "<verdict>", "LAI": '
            '"<verdict>", "OC": "<verdict>"}}}. '
            "Rules: worker_prompt is the single target the candidate is judged "
            "against and MAY differ in wording from BOTH render prompts. "
            "render_prompts.a and render_prompts.b are each a FULL standalone "
            "brief — a flawed side is a COMPLETE rewrite that embeds the flaw, "
            "NOT 'clean plus a note'. planted lists the concrete, VISIBLE flaws "
            "per side (wrong object counts, misspellings, extra/missing/floating "
            "elements); a flaw must be visible in the RENDERED image, never one "
            "that lives only in the prompt text. PER-DIMENSION MODEL: each "
            "planted flaw decides EXACTLY ONE dimension (IF, VQ, or LAI). You may "
            "flaw ONE side (set faithful_side to the OTHER, its planted list "
            "EMPTY) or BOTH sides (set faithful_side null, BOTH planted lists "
            "NON-EMPTY). A dimension NO flaw touches is 'Both Good'. A side that "
            "carries a flaw on one dimension MAY STILL WIN a DIFFERENT dimension "
            "(name that side) when the other side's flaw on that dimension is "
            "worse. When BOTH sides are flawed on the SAME dimension it is 'Both "
            "Bad'. construction_keys must cover EXACTLY IF, VQ, LAI, OC; each "
            "verdict is one of [Response A, Response B, Both Good, Both Bad], "
            "EXCEPT OC which is ONLY Response A or Response B and MUST ALWAYS be "
            "DECIDED to one side by a correctness-before-polish tiebreak (the "
            "side needing fewer corrections to be right wins) — OC is NEVER Both "
            "Good or Both Bad even when both sides are flawed. EVERY verdict must "
            "be JUSTIFIED by the planted flaws (the flaw that decides a dimension, "
            "or the absence of one for 'Both Good'/'Both Bad'). ACROSS THIS BATCH "
            "spread the planted flaws so every dimension (IF, VQ, LAI, OC) is a "
            "decisive verdict on at least one item rather than the default Both "
            "Good, no dimension carries the same verdict on every item, and which "
            "dimension is decisive varies item to item — a set where one "
            "dimension always ties is a planning failure. LEGACY shape still "
            "accepted (mapped automatically): flawed_side + clean_prompt + "
            "flawed_prompt + injected_flaws, but PREFER the worker_prompt / "
            "render_prompts / planted shape. Do NOT emit image_a_prompt / "
            "image_b_prompt or a free-form dimensions map; the platform derives "
            "the two images and the answer key from the flaw_plan."
        )
    if qtype == "image_prompt":
        return (
            'Shape: {"name": "...", "question_type": "image_prompt", "prompt": '
            '"...", "difficulty": "medium", "image_specs": {"images": [...], '
            '"answer_key": {"ideal_prompt": "...", "mandatory_elements": ["..."], '
            '"penalty_rules": ["..."], "scoring_guide": "..."}}}. TWO supported '
            "forms: (1) FROM-SCRATCH — exactly ONE image with slot \"single\"; the "
            "candidate WRITES the text-to-image prompt that would produce that "
            "image, graded against ideal_prompt. (2) TRANSFORMATION / COMPARE — "
            'exactly TWO images: one with slot "reference" (the starting / input '
            'image) and one with slot "output" (the target / result image); the '
            "candidate WRITES the transformation prompt that turns the reference "
            "INTO the output, and ideal_prompt describes that reference->output "
            "transformation. PREFER the 2-image form whenever the task is to edit, "
            "transform, restyle, or compare one image into another — the candidate "
            "must SEE both the reference and the output to write the prompt. Each "
            'image needs a REQUIRED detailed self-contained "prompt" brief. Show '
            "the candidate the rendered image(s); the stimulus must SHOW the "
            "evidence, never a caption that states the answer."
        )
    if qtype == "video_prompt":
        return (
            'Shape: {"name": "...", "question_type": "video_prompt", "prompt": '
            '"...", "difficulty": "medium", "image_specs": {"videos": [...], '
            '"answer_key": {"ideal_prompt": "...", "mandatory_elements": ["..."], '
            '"penalty_rules": ["..."], "scoring_guide": "..."}}}. The VIDEO twin '
            "of image_prompt. TWO supported forms. (1) FROM-SCRATCH — exactly ONE "
            'clip with slot "single"; the candidate WRITES the text-to-video '
            "prompt that would produce that clip, graded against ideal_prompt. "
            "(2) TRANSFORMATION / COMPARE (PREFER) — exactly TWO clips: one with "
            'slot "reference" (the starting / input clip) and one with slot '
            '"output" (the target / result clip); the candidate WRITES the '
            "transformation prompt that turns the reference clip INTO the output "
            "clip, and ideal_prompt describes that reference->output "
            "TRANSFORMATION. Prefer the 2-clip form whenever the task is to edit, "
            "restyle, re-time, or compare one clip into another — the candidate "
            "must SEE both the reference and the output to write the prompt. Each "
            'clip needs a REQUIRED detailed self-contained "prompt" brief stating '
            "subject, MOTION/action, camera, visual STYLE, SCENE STRUCTURE (any "
            "cut/scene divisions), background/lighting, DURATION, and any AUDIO / "
            "dialogue (or explicit silence). ideal_prompt must COVER the "
            "transformation as a checklist a grader can verify: name the shared "
            "STYLE, the concrete content CHANGES, the SCENE DIVISIONS, the "
            "AUDIO/SILENCE handling, any LENGTH change, and the DIALOGUE format. "
            "Show the candidate the clip(s); the stimulus must SHOW the evidence, "
            "never a caption that states the answer. The clips are UPLOADED by an "
            "admin or generated later — you author only the briefs + answer key."
        )
    return (
        'Shape: {"name": "...", "question_type": "image_label", "prompt": '
        '"...", "difficulty": "medium", "image_specs": {...}}. EXACTLY ONE image '
        'with slot "single" is shown; the candidate LABELS the elements in it. '
        "MANDATORY PRIMARY FORM — REAL-PAGE CAPTURE: for image_label you MUST "
        "return image_specs.source_url to a REAL, PUBLIC, non-login, reachable "
        "TOP-LEVEL page that actually renders the interactive controls the SOP "
        "is about. Do NOT invent a synthetic screenshot brief as the primary "
        "output and do NOT ask an image model to render a fake UI — a made-up "
        "screenshot has NO real DOM, so the platform cannot draw accurate "
        "numbered boxes on it. Choose the REAL site matching the SOP's app and "
        "seed from this golden set (pick the one whose product matches the SOP, "
        "or another equally stable public homepage): https://github.com, "
        "https://www.wikipedia.org, https://duckduckgo.com, "
        "https://www.canva.com, https://open.spotify.com — a public landing / "
        "homepage, NEVER a page behind login, paywall, or captcha. Emit "
        'image_specs = {"source_url": "<the REAL public URL>", "application": '
        '"<what app / site it is>", "viewport": {"width": 1440, "height": 900}, '
        '"wait_ms": 2500, "dismiss": ["<CSS selector for a cookie / consent '
        'ACCEPT button>", ...], "coverage_expected": "yes" | "no", "omit": '
        '{"match_tag": "...", "match_type": "...", "match_text": "..."}, '
        '"images": [{"slot": "single", "label": "Screenshot", "prompt": "<a '
        "DENSE synthetic screenshot brief describing the SAME page, used by the "
        "platform ONLY as an automatic FALLBACK if the live capture is "
        'unavailable>"}]}. To keep that fallback SELF-CONTAINED and always '
        "LABELLED (the live capture may be unavailable / rate-limited), you MUST "
        "ALSO author the DENSE per-box map for that SAME synthetic screenshot: a "
        '"boxes" list [{"number": 1, "box_2d": [ymin,xmin,ymax,xmax] on a 0-1000 '
        "grid locating the control in the screenshot your images[0].prompt "
        'describes, "element": "<short name>", "functionality": "<the ACTION it '
        'performs>"}, ..., {"number": N, ...}] PLUS "answer_key": {"ideal_labels": '
        '{"1": "<functionality of box 1>", ..., "N": "<functionality of box N>"}} '
        "keyed by box number — EXACTLY as in the DENSE form (1) below. Then if the "
        "platform ever renders the synthetic fallback it draws the numbered boxes "
        "from YOUR coordinates deterministically with ZERO extra detection calls, "
        "so the candidate always sees a clean numbered screenshot plus the "
        "matching ideal_labels key. The platform drives a headless browser to "
        "screenshot source_url and draw numbered boxes at the REAL element "
        "geometry with a "
        "mechanically-drafted behavioural key (ground truth BY CONSTRUCTION, "
        "ZERO model inference); the boxes/answer key are NOT authored by you for "
        "this form. source_url is REQUIRED; the images brief is SECONDARY and "
        "exists ONLY as the documented synthetic fallback — ALWAYS include BOTH, "
        "source_url first. dismiss lists the cookie / consent ACCEPT selectors "
        "to click so an overlay does not hide the controls; wait_ms is the "
        "settle delay. coverage_expected + omit leave ONE interactive element "
        "deliberately unboxed so the coverage answer is \"No\" BY CONSTRUCTION "
        "(omit works exactly as in the dense fallback form below). The remaining "
        "forms are the SYNTHETIC FALLBACK shapes the platform renders only when "
        "capture is unavailable — still author the images brief above so a "
        "fallback exists, but never emit them WITHOUT a source_url. (1) DENSE "
        "SCREENSHOT LABELLING (the fallback shape for an app / "
        "website / UI screenshots): the ONE image brief depicts an interface "
        "carrying MULTIPLE (5-15) interactive elements, and you number and label "
        "EVERY one of them. Emit image_specs = {\"images\": [{\"slot\": "
        '"single", "label": "Screenshot", "prompt": "<detailed self-contained '
        'brief for a single screenshot showing every listed interactive '
        'element, quoting each visible control label verbatim>"}], '
        '"application": "<what app / site the screenshot depicts>", '
        '"coverage_expected": "yes" | "no", "boxes": [{"number": 1, "box_2d": '
        "[ymin,xmin,ymax,xmax], \"element\": \"<short name of the control>\", "
        '"functionality": "<the ACTION it performs, e.g. \'Opens the cart\'>"}, '
        "..., {\"number\": N, ...}], \"answer_key\": {\"ideal_labels\": "
        '{"1": "<functionality of box 1>", ..., "N": "<functionality of box '
        'N>"}, "mandatory_elements": ["..."], "penalty_rules": ["..."], '
        '"scoring_guide": "..."}}. Rules for the dense form: box_2d is an '
        "APPROXIMATE normalized rectangle [ymin,xmin,ymax,xmax] on a 0-1000 grid "
        "locating that control in your briefed screenshot (top-left origin); the "
        "platform draws the numbered boxes from these coordinates, so place one "
        "box per interactive control and keep them in reading order "
        "(top-to-bottom, left-to-right). functionality grades what the control "
        "DOES, not merely its name. answer_key.ideal_labels is the PER-BOX MAP "
        "keyed by box number to the same functionality. coverage_expected is "
        '"yes" when EVERY interactive element in the briefed screenshot has a '
        'box; set it "no" ONLY when you deliberately leave ONE interactive '
        'element un-boxed, and then also emit "omitted_element": {"tag": "...", '
        '"text": "...", "reason": "..."} naming the element you left out, so the '
        'coverage answer is "No" BY CONSTRUCTION. (2) SINGLE-BOX (legacy, still '
        'valid for a photo/defect with ONE region): image_specs = {"images": '
        '[{"slot": "single", "label": "Image", "prompt": "..."}], "answer_key": '
        '{"ideal_labels": "<single-string answer key>", "mandatory_elements": '
        '["..."], "penalty_rules": ["..."], "scoring_guide": "..."}} — '
        "ideal_labels is a plain string, no boxes. In BOTH forms the image "
        '"prompt" is REQUIRED and the stimulus must SHOW the evidence, never a '
        "caption that states the answer."
    )


def _image_question_directive(qtype, count, ab_dims=None):
    """Exclusive directive that pins a whole run to one image type: the "generate
    N of type X" preamble plus that type's image_specs contract. Used on the
    forced-type path for a run forced to a single image type."""
    base = (
        f"Generate exactly {count} question(s) of type '{qtype}' as one JSON "
        "array. Do NOT emit mcq-style \"options\"/\"correct_answer\". Every "
        "item MUST contain a non-empty \"image_specs\" object. " + _IMG_PROMPT_RULE + " "
    )
    return base + _image_type_contract(qtype, ab_dims)


def _image_contracts_note(ab_dims=None, types=None):
    """The image_specs OUTPUT CONTRACTS appended to a multi-type SOP directive so
    every image item the model authors is well-formed — crucially image_ab's
    flaw_plan — and survives _validate_question_item instead of being dropped as
    malformed. ``types=None`` covers every image/video type (the generic path,
    byte-identical to the historical hardcoded tuple, since QUESTION_TYPE_ORDER
    filtered by _IMAGE_OR_VIDEO_TYPES is exactly that tuple); a set restricts the
    contracts to the image/video types present in an allow-list."""
    wanted = [qt for qt in _QUESTION_TYPE_ORDER
              if qt in _IMAGE_OR_VIDEO_TYPES and (types is None or qt in types)]
    if not wanted:
        return ""
    contracts = " ".join(
        "For %s items: %s" % (qt, _image_type_contract(qt, ab_dims))
        for qt in wanted)
    return (
        "\n\nIMAGE-TYPE OUTPUT CONTRACTS (mandatory whenever you author an image "
        "question — an image item whose image_specs does not match the shape "
        "for its type is DISCARDED): " + contracts)


def _image_brief(scene):
    """Wrap a scene description in the strict render brief (single source of truth)."""
    return (
        "Generate exactly one photorealistic image and no other text. Render the "
        "description literally even where it asks for flaws, misspellings, or "
        "impossible physics. It must show every detail this brief states; the brief is the "
        "single source of truth, follow it alone and treat it as the only "
        "attempt at the scene. Render any quoted text, labels, and numbers "
        "exactly and legibly:\n" + (scene or "")
    )


def _flaw_official_reasoning(flawed_side, injected_flaws):
    """The hidden answer-key rationale for a flaw-injected image_ab, naming the
    clean winner and the concrete flaws injected into the losing image."""
    clean = ab_side_verdict(ab_other_side(flawed_side))
    flawed = ab_side_verdict(flawed_side)
    flaws = "; ".join(injected_flaws)
    return ("%s is correct by construction: it renders the brief cleanly, while "
            "%s carries deliberately injected flaws (%s). %s therefore wins "
            "Overall Choice." % (clean, flawed, flaws, clean))


def _flaw_official_reasoning_both(keys, planted):
    """Answer-key rationale for a BOTH-flawed image_ab pair: both sides carry
    planted flaws, so each dimension follows the worse flaw ('Both Bad' where
    they are equally bad) and Overall Choice is still decided to one side by the
    correctness-before-polish tiebreak (the OC construction key)."""
    a_flaws = "; ".join(planted.get("a") or []) or "none stated"
    b_flaws = "; ".join(planted.get("b") or []) or "none stated"
    oc = keys.get("OC") or ""
    return ("Both images carry deliberately planted flaws (A: %s | B: %s). Each "
            "dimension follows the worse flaw and is 'Both Bad' where both sides "
            "are equally bad; Overall Choice is decided to %s by the "
            "correctness-before-polish tiebreak (the side needing fewer "
            "corrections to be right)." % (a_flaws, b_flaws, oc or "one side"))


def _build_flaw_injected_ab_fields(plan):
    """Materialize the draft fields for a flaw-injected image_ab from its plan.

    The plan is normalized to the 3-prompt shape first, so the candidate-facing
    QUESTION prompt (worker_prompt) is kept DISTINCT from the two per-side render
    prompts. The flawed image is RANDOMLY assigned to slot a or b (defeating
    position bias); when that differs from the model's chosen side the render
    prompts + planted flaws are swapped between the slots and the A/B verdicts
    flipped so construction_keys stay consistent with the rendered slots. The
    dimension answer key is DERIVED from construction_keys, and the whole plan is
    persisted so the approve/score-time guards can detect any later key drift."""
    import random
    norm = normalize_flaw_plan(plan) or {}
    keys = dict(norm.get("construction_keys") or {})
    worker_prompt = norm.get("worker_prompt") or ""
    render_prompts = dict(norm.get("render_prompts") or {"a": "", "b": ""})
    planted = {"a": list((norm.get("planted") or {}).get("a") or []),
               "b": list((norm.get("planted") or {}).get("b") or [])}
    both_flawed = norm.get("faithful_side") not in AB_FLAWED_SIDES
    if both_flawed:
        do_swap = random.choice((False, True))
    else:
        do_swap = random.choice(AB_FLAWED_SIDES) != (norm.get("flawed_side") or "")
    if do_swap:
        render_prompts = {"a": render_prompts.get("b") or "",
                          "b": render_prompts.get("a") or ""}
        planted = {"a": planted["b"], "b": planted["a"]}
        keys = ab_flip_construction_keys(keys)
    if both_flawed:
        final_faithful = "both"
        final_flawed = "both"
        injected = planted["a"] + planted["b"]
        reasoning = _flaw_official_reasoning_both(keys, planted)
        flawed_prompt = ""
    else:
        final_flawed = norm.get("flawed_side") if not do_swap \
            else ab_other_side(norm.get("flawed_side"))
        final_faithful = ab_other_side(final_flawed)
        injected = planted[final_flawed]
        reasoning = _flaw_official_reasoning(final_flawed, injected)
        flawed_prompt = render_prompts.get(final_flawed) or ""
    briefs = [
        {"slot": "a", "label": "Response A", "prompt": render_prompts.get("a") or ""},
        {"slot": "b", "label": "Response B", "prompt": render_prompts.get("b") or ""},
    ]
    vals = {
        "image_brief_json": json.dumps(briefs, ensure_ascii=False),
        "dimensions_json": json.dumps(
            ab_specs_from_construction_keys(keys), ensure_ascii=False),
        "flaw_plan_json": json.dumps({
            "faithful_side": final_faithful,
            "flawed_side": final_flawed,
            "worker_prompt": worker_prompt,
            "render_prompts": render_prompts,
            "planted": planted,
            "construction_keys": keys,
            "clean_prompt": worker_prompt,
            "flawed_prompt": flawed_prompt,
            "injected_flaws": injected,
        }, ensure_ascii=False),
        "official_reasoning": reasoning,
    }
    # DERIVE the golden solution verdicts FROM the authoritative construction_keys
    # (which are slot-aligned, and flipped above when the flawed side was swapped),
    # so the stored golden answer can NEVER contradict the rendered images. The
    # model's separately-written solution verdicts are NOT trusted here — a
    # thinking model sometimes writes them for the pre-swap side or self-
    # contradicts, which would grade a worker against a wrong key (observed:
    # ~40% of flaw drafts had an A/B label mismatch). rationale is preserved when
    # the model supplied one; the verdicts are always the construction ground truth.
    _CK2SOL = {"IF": "instruction_following", "VQ": "visual_quality",
               "LAI": "less_ai_generated", "OC": "overall_preference"}
    derived = {_CK2SOL[k]: v for k, v in keys.items() if k in _CK2SOL}
    vals["_derived_ab_solution"] = derived
    if worker_prompt:
        vals["question_prompt"] = worker_prompt
    return vals


_IMAGE_PROMPT_SLOT_ALIASES = {
    "reference": "reference", "ref": "reference", "input": "reference",
    "source": "reference", "style": "reference", "before": "reference",
    "style-reference": "reference", "style reference": "reference",
    "output": "output", "out": "output", "target": "output",
    "result": "output", "after": "output", "transformed": "output",
    "single": "single", "image": "single", "img": "single",
}
_IMAGE_PROMPT_SLOT_ORDER = {"reference": 0, "output": 1, "single": 2}
_IMAGE_PROMPT_SLOT_LABEL = {"reference": "Reference", "output": "Output",
                            "single": "Image"}


def _image_prompt_briefs(images):
    """Render briefs for an image_prompt item, honouring the reference/output
    roles so a transform/compare task renders BOTH images (not just the first).

    Each image_specs image carrying a non-empty prompt becomes one brief. A
    SINGLE image maps to slot 'single' (from-scratch: write the prompt for this
    one image); TWO or more map to 'reference' then 'output' — honouring any slot
    the model set and filling the rest in that order — so the reference (input)
    always precedes the output (target). Returns ``[{slot,label,prompt}]``."""
    imgs = [i for i in images
            if isinstance(i, dict) and (i.get("prompt") or "").strip()]
    if not imgs:
        return []
    briefs = [{
        "slot": _IMAGE_PROMPT_SLOT_ALIASES.get(
            (i.get("slot") or "").strip().lower(), ""),
        "label": (i.get("label") or "").strip(),
        "prompt": i["prompt"],
    } for i in imgs]
    if len(briefs) == 1:
        briefs[0]["slot"] = briefs[0]["slot"] or "single"
    else:
        default_order = ["reference", "output"]
        used = {b["slot"] for b in briefs if b["slot"]}
        di = 0
        for b in briefs:
            if b["slot"]:
                continue
            while di < len(default_order) and default_order[di] in used:
                di += 1
            b["slot"] = default_order[di] if di < len(default_order) else "output"
            used.add(b["slot"])
            di += 1
    briefs.sort(key=lambda b: _IMAGE_PROMPT_SLOT_ORDER.get(b["slot"], 9))
    for b in briefs:
        b["label"] = b["label"] or _IMAGE_PROMPT_SLOT_LABEL.get(
            b["slot"], b["slot"].title())
    return briefs


def _video_prompt_briefs(videos):
    """Render briefs for a video_prompt item — the video twin of
    _image_prompt_briefs. Each clip brief carrying a non-empty prompt becomes one
    brief; a SINGLE clip maps to slot 'single' (from-scratch), TWO or more map to
    'reference' then 'output' (the transform/compare pair) honouring any slot the
    model set, so the reference clip always precedes the output clip. The clip
    slot vocabulary is identical to image_prompt's, so this reuses the same
    role-resolution logic. Returns ``[{slot,label,prompt}]``."""
    return _image_prompt_briefs(videos)


def _label_key_sort(k):
    """Sort per-box map keys numerically, with non-numeric keys last."""
    try:
        return (0, int(k))
    except (TypeError, ValueError):
        return (1, str(k))


def _normalize_box_2d(box):
    """Coerce a model-supplied box to ``[ymin,xmin,ymax,xmax]`` ints clamped to
    the 0-1000 grid (Gemini/annotate_image space), or None when malformed."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        coords = [int(round(float(c))) for c in box]
    except (TypeError, ValueError):
        return None
    return [max(0, min(1000, c)) for c in coords]


def _image_label_boxes(specs):
    """Ordered per-box plan for a DENSE image_label from ``image_specs``.

    Reads the model's ``boxes`` list ([{number?, box_2d, element|label,
    functionality|description}]) and, where a box omits its functionality, fills
    it from the ``answer_key.ideal_labels`` per-box MAP ({number: functionality}).
    Only boxes carrying a VALID box_2d are kept — we can draw a numbered box only
    where the model told us it sits — and they are renumbered 1..N in the given
    reading order. Returns ``[{number, box_2d, element, functionality}]`` (empty
    when the item is not dense, i.e. no boxes with coordinates)."""
    answer_key = specs.get("answer_key") or {}
    ideal = answer_key.get("ideal_labels") if isinstance(answer_key, dict) else None
    labels_map = ideal if isinstance(ideal, dict) else {}
    raw_boxes = specs.get("boxes")
    entries = []
    if isinstance(raw_boxes, list):
        for b in raw_boxes:
            if not isinstance(b, dict):
                continue
            box_2d = _normalize_box_2d(b.get("box_2d") or b.get("box"))
            if not box_2d:
                continue
            n = len(entries) + 1
            element = str(b.get("element") or b.get("label") or "").strip()
            func = str(
                b.get("functionality") or b.get("description")
                or labels_map.get(str(b.get("number") or n))
                or labels_map.get(str(n)) or "").strip()
            entries.append({
                "number": n,
                "box_2d": box_2d,
                "element": element or ("Element %d" % n),
                "functionality": func,
            })
    return entries


# Golden-set seeds used to REPAIR an image_label that omitted its (now
# mandatory) source_url: map the model's stated application / site — or a site
# name leaking through the synthetic brief — to a real, public, stable homepage
# so the real-page DOM-capture path still runs instead of a synthetic render.
# Ordered most-specific first; each host matches when any of its needles appears.
_IMAGE_LABEL_URL_SEEDS = (
    ("https://open.spotify.com", ("spotify",)),
    ("https://github.com", ("github",)),
    ("https://www.wikipedia.org", ("wikipedia", "wikimedia")),
    ("https://duckduckgo.com", ("duckduckgo", "duck duck go")),
    ("https://www.canva.com", ("canva",)),
    ("https://www.youtube.com", ("youtube",)),
    ("https://www.amazon.com", ("amazon",)),
    ("https://www.google.com", ("google search", "google.com")),
)


def _repair_image_label_source_url(specs):
    """Derive a real public source_url for an image_label item that omitted it.

    source_url is MANDATORY for image_label, but generation must never fail
    outright: when the model authored a synthetic brief with no source_url we try
    to recover one from the stated ``application`` (or a known site name leaking
    through the synthetic ``images`` brief) using the golden-set seeds, so the
    real-page capture path runs instead of the fake-screenshot path. Returns a
    URL string, or "" when nothing in the item names a known public site (the
    caller then keeps the synthetic brief as the genuine fallback)."""
    if not isinstance(specs, dict):
        return ""
    hay = (str(specs.get("application") or "") + " "
           + str(specs.get("source_url") or "")).lower()
    for img in (specs.get("images") or []):
        if isinstance(img, dict):
            hay += " " + str(img.get("prompt") or "").lower()
            hay += " " + str(img.get("label") or "").lower()
    for host, needles in _IMAGE_LABEL_URL_SEEDS:
        if any(n in hay for n in needles):
            return host
    return ""


def _apply_capture_directives(specs, vals):
    """Persist the REAL-PAGE CAPTURE directives of an image_label ``image_specs``
    into ``vals``: the url, a capture_config_json (viewport / wait_ms / dismiss),
    an omit_spec_json, coverage_expected and label_application.

    source_url is MANDATORY for image_label; when the model omitted it we try to
    REPAIR one from the stated application via the golden-set seeds so the draft
    still captures a real page. No-op only when there is no source_url AND none
    can be derived (the item then relies on the synthetic fallback)."""
    src = str(specs.get("source_url") or "").strip()
    if not src:
        src = _repair_image_label_source_url(specs)
    if not src:
        return
    vals["source_url"] = src[:2000]
    # A source_url means the label target is a live web page / app UI, so the
    # detection prompt must be the UI-element one (not the physical-object one).
    # This makes the synthetic fallback + any Gemini detect use the right prompt,
    # and matches the live DOM capture that is now the render-time default.
    vals["detection_mode"] = "ui"
    cfg: dict = {}
    vp = specs.get("viewport")
    if isinstance(vp, dict):
        try:
            w, h = int(vp.get("width") or 0), int(vp.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        if w > 0 and h > 0:
            cfg["viewport"] = {"width": w, "height": h}
    if specs.get("wait_ms") is not None:
        try:
            cfg["wait_ms"] = int(specs.get("wait_ms"))
        except (TypeError, ValueError):
            pass
    dismiss = specs.get("dismiss")
    if isinstance(dismiss, list):
        sels = [str(s).strip() for s in dismiss if str(s).strip()]
        if sels:
            cfg["dismiss"] = sels
    if cfg:
        vals["capture_config_json"] = json.dumps(cfg, ensure_ascii=False)
    omit = specs.get("omit")
    if isinstance(omit, dict) and omit:
        vals["omit_spec_json"] = json.dumps(omit, ensure_ascii=False)
    cov = str(specs.get("coverage_expected") or "yes").strip().lower()
    vals["coverage_expected"] = "no" if cov == "no" else "yes"
    app = str(specs.get("application") or "").strip()
    if app:
        vals["label_application"] = app[:200]


def _image_label_draft_fields(specs):
    """Answer-key + image-brief draft fields for one image_label item.

    DENSE form (reference proj-2 screenshot labelling): the model authored a
    single screenshot showing MULTIPLE interactive elements, an ordered ``boxes``
    plan (each with approximate normalized coordinates + the ACTION it performs),
    an ``application`` name and a coverage expectation. We persist that as the
    Phase-4 shapes — ``behavioural_key_json`` (the per-box action key),
    ``label_boxes_json`` (the numbered-box geometry the overlay is drawn from at
    approve, reusing imaging.annotate_image — no second Vertex/detection call),
    ``coverage_expected`` / ``omitted_element_json`` (the coverage gate) and
    ``label_application`` — so the candidate UI and scoring treat model-authored
    boxes exactly like DOM-captured ones. ``ideal_labels`` is flattened to a
    readable STRING in rubric_json so the answer-key editor and the legacy
    scoring fallback keep working; the per-box map lives in the behavioural key.

    REAL-PAGE CAPTURE (preferred) form: when the model named a ``source_url``, we
    persist it plus a ``capture_config_json`` (viewport / wait_ms / dismiss),
    ``omit_spec_json``, ``coverage_expected`` and ``label_application`` so the
    detect cron drives a live DOM capture; the dense/single ``images`` brief is
    ALSO persisted (image_brief_json + any boxes) as the synthetic FALLBACK the
    hybrid path renders when capture is unavailable or fails.

    SINGLE-BOX (legacy) form: a lone region + a single-string ideal_labels answer
    key, unchanged (no boxes, no behavioural key, detection runs post-approve)."""
    vals = {}
    _apply_capture_directives(specs, vals)
    briefs = []
    for img in (specs.get("images") or []):
        if img.get("prompt"):
            briefs.append({
                "slot": "single",
                "label": img.get("label") or "Image",
                "prompt": img["prompt"],
            })
            break
    answer_key = dict(specs.get("answer_key") or {})
    boxes = _image_label_boxes(specs)
    ideal = answer_key.get("ideal_labels")
    if isinstance(ideal, dict):
        answer_key["ideal_labels"] = "\n".join(
            "%s. %s" % (k, ideal[k]) for k in sorted(ideal, key=_label_key_sort))
    if boxes:
        vals["behavioural_key_json"] = json.dumps(
            [{"number": b["number"], "element": b["element"],
              "functionality": b["functionality"]} for b in boxes],
            ensure_ascii=False)
        vals["label_boxes_json"] = json.dumps(
            [{"number": b["number"], "box_2d": b["box_2d"],
              "label": b["element"], "description": b["functionality"]}
             for b in boxes], ensure_ascii=False)
        answer_key["ideal_labels"] = "\n".join(
            "%d. %s" % (b["number"], b["functionality"]) for b in boxes)
        cov = str(specs.get("coverage_expected") or "yes").strip().lower()
        vals["coverage_expected"] = "no" if cov == "no" else "yes"
        omitted = specs.get("omitted_element")
        if cov == "no" and isinstance(omitted, dict) and omitted:
            vals["omitted_element_json"] = json.dumps(omitted, ensure_ascii=False)
        app = str(specs.get("application") or "").strip()
        if app:
            vals["label_application"] = app[:200]
    if answer_key:
        vals["rubric_json"] = json.dumps(answer_key, ensure_ascii=False)
    if briefs:
        vals["image_brief_json"] = json.dumps(briefs, ensure_ascii=False)
    return vals


def _build_image_draft_fields(env, qtype, item, usage_ctx=None, ab_dims=None):
    """Build the answer-key + image-brief fields for one image draft (no rendering here)."""
    specs = item.get("image_specs") or {}
    vals = {}
    briefs = []

    if qtype == "image_ab":
        plan = specs.get("flaw_plan") or {}
        if plan and not validate_flaw_plan(plan):
            return _build_flaw_injected_ab_fields(plan)
        if specs.get("image_a_prompt"):
            briefs.append({"slot": "a", "label": "Response A",
                           "prompt": specs["image_a_prompt"]})
        if specs.get("image_b_prompt"):
            briefs.append({"slot": "b", "label": "Response B",
                           "prompt": specs["image_b_prompt"]})
        resolved = ab_dims or _ab_fallback_dims()
        by_label = {d["label"].strip().lower(): list(d["choices"])
                    for d in resolved}
        union = []
        for d in resolved:
            for c in d["choices"]:
                if c not in union:
                    union.append(c)
        dim_specs = []
        for label, val in (specs.get("dimensions") or {}).items():
            choices = by_label.get(str(label).strip().lower()) or union \
                or list(_AB_CHOICES)
            dim_specs.append({
                "label": label,
                "options": choices,
                "correct": [val] if val else [],
            })
        if dim_specs:
            vals["dimensions_json"] = json.dumps(dim_specs, ensure_ascii=False)
        if specs.get("official_reasoning"):
            vals["official_reasoning"] = specs["official_reasoning"]
    elif qtype == "image_prompt":
        briefs = _image_prompt_briefs(specs.get("images") or [])
        answer_key = specs.get("answer_key") or {}
        if answer_key:
            vals["rubric_json"] = json.dumps(answer_key, ensure_ascii=False)
    elif qtype == "video_prompt":
        v_briefs = _video_prompt_briefs(
            specs.get("videos") or specs.get("images") or [])
        answer_key = specs.get("answer_key") or {}
        if answer_key:
            vals["rubric_json"] = json.dumps(answer_key, ensure_ascii=False)
        if v_briefs:
            vals["video_brief_json"] = json.dumps(v_briefs, ensure_ascii=False)
        return vals
    else:
        return _image_label_draft_fields(specs)

    if briefs:
        vals["image_brief_json"] = json.dumps(briefs, ensure_ascii=False)
    return vals


def render_draft_images(env, briefs, usage_ctx=None, only_slot=None):
    """Render briefs -> ``[{slot,label,data}]``; per-brief failures are skipped.

    On-demand Model 2 step, never run from the generate-questions request.
    ``only_slot`` (when set) renders just that slot.

    MONEY SAFETY: when a 429 aborts the loop, the slots already rendered in this
    call are NOT thrown away — they are attached to the raised VertexQuotaError
    as ``.partial`` so the caller can persist them and re-render only what's
    still missing, never re-paying for a picture that already came back.
    """
    images = []
    for brief in (briefs or []):
        if not isinstance(brief, dict):
            continue
        slot = brief.get("slot") or "single"
        if only_slot and slot != only_slot:
            continue
        prompt_text = brief.get("prompt")
        if not prompt_text:
            continue
        try:
            b64, mime = generate_image(
                env, _image_brief(prompt_text), usage_ctx=usage_ctx)
            images.append({
                "slot": slot,
                "label": brief.get("label") or slot.title(),
                "data": "data:%s;base64,%s" % (mime, b64),
            })
        except VertexQuotaError as exc:
            # Keep the paid slots: hand them back on the exception so the caller
            # can save progress and only retry the missing slots next tick.
            exc.partial = images
            raise
        except Exception as exc:
            _logger.warning(
                "etp_assessment image render failed (%s): %s",
                slot, repr(exc)[:160])
    return images


_VERIFY_MAX_REGEN = 2

_VERIFY_SYSTEM_PROMPT = (
    "You are a meticulous visual-QA inspector for an assessment pipeline. You "
    "are shown ONE rendered image plus a numbered list of specific flaws that "
    "were DELIBERATELY planted into it. For EACH listed flaw, decide ONLY from "
    "what is actually visible in the pixels whether that flaw is present. Do NOT "
    "assume a flaw is there merely because it is listed, and do NOT invent flaws "
    "that are not listed. Return ONLY the JSON array the schema describes (one "
    "entry per listed flaw, in the SAME order), no markdown, no prose."
)

VERIFY_FLAW_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "flaw": {"type": "STRING"},
            "present": {"type": "BOOLEAN"},
            "note": {"type": "STRING"},
        },
        "required": ["flaw", "present"],
    },
}


def _verify_model(env):
    """Vision/doc-capable model for flaw verification: reuses the detection model
    (a multimodal model that can READ an image), never the image-only render
    model. Overridable via etp_assessment_pro.verify_model."""
    return _param(env, "etp_assessment_pro.verify_model") \
        or _detection_model(env)


def verify_planted_flaws(env, image, planted_flaws, *, model=None,
                         usage_ctx=None):
    """One multimodal Gemini call checking whether each planted flaw is VISIBLE
    in the rendered image.

    ``image`` is raw bytes OR a base64 string (PNG); ``planted_flaws`` is the
    flaw list for ONE side. Returns
    ``{"verdicts": [{"flaw","present","note"}], "all_present": bool}`` with one
    verdict per planted flaw (order preserved; a flaw the model omits is treated
    as NOT present — the correctness-safe default so an unrendered flaw never
    silently passes). Parses defensively. Raises VertexQuotaError on 429 so the
    caller can self-heal; other Vertex failures propagate so the caller degrades
    gracefully."""
    flaws = [str(f).strip() for f in (planted_flaws or []) if str(f).strip()]
    if not flaws:
        return {"verdicts": [], "all_present": True}
    import base64
    if isinstance(image, (bytes, bytearray)):
        b64 = base64.b64encode(bytes(image)).decode()
    else:
        b64 = str(image)
    numbered = "\n".join("%d. %s" % (i + 1, f) for i, f in enumerate(flaws))
    user_parts = [
        {"inlineData": {"mimeType": "image/png", "data": b64}},
        {"text": "These flaws were deliberately planted into this image. For "
                 "each, report whether it is visibly present in the pixels:\n"
                 + numbered},
    ]
    raw = _call_vertex(
        env, _VERIFY_SYSTEM_PROMPT, user_text="", user_parts=user_parts,
        model=model or _verify_model(env), max_tokens=4000, temperature=0.0,
        response_json=True, response_schema=VERIFY_FLAW_SCHEMA,
        usage_ctx=usage_ctx or {"operation": "verify_planted_flaws"})
    items = [it for it in _extract_json_array(raw) if isinstance(it, dict)]
    verdicts = []
    for i, flaw in enumerate(flaws):
        present, note = False, ""
        if i < len(items):
            present = bool(items[i].get("present"))
            note = str(items[i].get("note") or "").strip()
        else:
            for it in items:
                lbl = str(it.get("flaw") or "").strip().lower()
                if lbl and (lbl in flaw.lower() or flaw.lower() in lbl):
                    present = bool(it.get("present"))
                    note = str(it.get("note") or "").strip()
                    break
        verdicts.append({"flaw": flaw, "present": present, "note": note})
    return {"verdicts": verdicts,
            "all_present": all(v["present"] for v in verdicts)}


def _data_url_to_b64(data):
    """Strip a possible ``data:<mime>;base64,`` prefix, returning bare base64."""
    s = data or ""
    if s.startswith("data:") and "base64," in s:
        return s.split("base64,", 1)[1]
    return s


def verify_and_regenerate_ab_images(env, briefs, images, planted, *,
                                    usage_ctx=None, max_regen=_VERIFY_MAX_REGEN):
    """VERIFY->REGENERATE loop for a rendered image_ab pair (Phase 3).

    For each side ('a','b') that carries planted flaws, verify they are visible
    in that side's rendered image; if any is ABSENT, RE-RENDER just that slot
    (reusing its brief + the literal render directive via ``render_draft_images``)
    up to ``max_regen`` times, re-verifying after each. Returns
    ``(images, record)``: ``images`` is the (possibly re-rendered) list in the
    original slot order; ``record`` documents every side plus ``all_confirmed``
    and ``needs_review``. VertexQuotaError propagates (caller re-queues). A
    NON-quota verify failure (creds absent, refusal) marks that side
    ``unavailable`` and leaves its image untouched — verification degrades,
    generation is never broken."""
    by_slot = {img["slot"]: img for img in (images or [])
               if isinstance(img, dict) and img.get("slot")}
    sides = {}
    for slot in AB_FLAWED_SIDES:
        flaws = [str(f).strip() for f in (planted.get(slot) or [])
                 if str(f).strip()]
        if not flaws:
            sides[slot] = {"planted": [], "regenerations": 0,
                           "confirmed": True, "unavailable": False,
                           "verdicts": []}
            continue
        img = by_slot.get(slot)
        regen, result, unavailable = 0, None, False
        while True:
            b64 = _data_url_to_b64(img.get("data")) if img else ""
            if not b64:
                result = {"verdicts": [{"flaw": f, "present": False,
                                        "note": "no rendered image"}
                                       for f in flaws],
                          "all_present": False}
                break
            try:
                result = verify_planted_flaws(
                    env, b64, flaws, usage_ctx=usage_ctx)
            except VertexQuotaError:
                raise
            except Exception as exc:  # noqa: BLE001 - degrade, never break gen
                _logger.warning(
                    "etp_assessment flaw verify unavailable (slot %s): %s",
                    slot, repr(exc)[:160])
                unavailable = True
                break
            if result["all_present"] or regen >= max_regen:
                break
            new = render_draft_images(
                env, briefs, usage_ctx=usage_ctx, only_slot=slot)
            regen += 1
            if new:
                img = new[0]
                by_slot[slot] = new[0]
        if unavailable or result is None:
            sides[slot] = {"planted": flaws, "regenerations": regen,
                           "confirmed": False, "unavailable": True,
                           "verdicts": []}
        else:
            sides[slot] = {"planted": flaws, "regenerations": regen,
                           "confirmed": result["all_present"],
                           "unavailable": False,
                           "verdicts": result["verdicts"]}
    new_images = [by_slot.get(img["slot"], img) if isinstance(img, dict)
                  and img.get("slot") else img for img in (images or [])]
    needs_review = any(
        (not s["confirmed"]) and (not s["unavailable"]) and s["planted"]
        for s in sides.values())
    all_confirmed = all(s["confirmed"] for s in sides.values() if s["planted"])
    record = {
        "enabled": True,
        "max_regen": max_regen,
        "sides": sides,
        "all_confirmed": bool(all_confirmed),
        "needs_review": bool(needs_review),
    }
    return new_images, record


def _answer_resolves(ca, options, single):
    """True if ``ca`` resolves against ``options`` as a string or 0-based index.

    ``single`` requires exactly one value (mcq); else a non-empty subset (msq).
    """
    n = len(options)
    opt_strs = [str(o) for o in options]

    def one_ok(v):
        if isinstance(v, bool):
            return False
        if isinstance(v, str):
            if v in opt_strs:
                return True
            s = v.strip()
            return s.lstrip("-").isdigit() and 0 <= int(s) < n
        if isinstance(v, int):
            return 0 <= v < n
        return False

    if single:
        if isinstance(ca, list):
            return len(ca) == 1 and one_ok(ca[0])
        return one_ok(ca)
    vals = ca if isinstance(ca, list) else ([ca] if ca is not None else [])
    return bool(vals) and all(one_ok(v) for v in vals)


def _resolved_correct_count(ca, options):
    """How many DISTINCT options ``ca`` marks correct (string match or 0-based
    index). Used to reject an msq that marks every option correct — a
    non-discriminating question."""
    n = len(options)
    opt_strs = [str(o) for o in options]
    idxs = set()
    vals = ca if isinstance(ca, list) else ([ca] if ca is not None else [])
    for v in vals:
        if isinstance(v, bool):
            continue
        if isinstance(v, str):
            if v in opt_strs:
                idxs.add(opt_strs.index(v))
            else:
                s = v.strip()
                if s.lstrip("-").isdigit() and 0 <= int(s) < n:
                    idxs.add(int(s))
        elif isinstance(v, int) and 0 <= v < n:
            idxs.add(v)
    return len(idxs)


def _is_decisive_ab_winner(winner):
    """True when an image_ab dimension verdict decides a side (Response A/B),
    as opposed to a tie (Both Good / Both Bad). Used to reject a flaw-free A/B
    where every dimension is a tie (no discriminating ground truth)."""
    w = str(winner or "").strip().lower()
    return w in ("response a", "response b", "a", "b")


def _validate_question_item(it, qtype, ab_dims=None):
    """Return contract violations for one generated item (empty == valid), so malformed items are skipped."""
    errs = []
    if not (it.get("prompt") or it.get("name") or "").strip():
        errs.append("no prompt/name")
    if qtype == "mcq":
        opts = it.get("options") or []
        ca = it.get("correct_answer")
        if not isinstance(opts, list) or len(opts) < 2:
            errs.append("mcq needs >=2 options")
        elif not _answer_resolves(ca, opts, single=True):
            errs.append("mcq correct_answer must be one option (string or index)")
    elif qtype == "msq":
        opts = it.get("options") or []
        ca = it.get("correct_answer")
        if not isinstance(opts, list) or len(opts) < 2:
            errs.append("msq needs >=2 options")
        elif not _answer_resolves(ca, opts, single=False):
            errs.append("msq correct_answer must be a non-empty subset of options")
        elif _resolved_correct_count(ca, opts) >= len(opts):
            # EVERY option marked correct = no discrimination (a non-question).
            errs.append("msq correct_answer marks ALL options correct "
                        "(needs at least one wrong option)")
    elif qtype == "subjective_rubric":
        r = it.get("rubric")
        if not isinstance(r, dict) or not all(
                k in r for k in ("checklist", "constraints", "pass_condition")):
            errs.append("subjective_rubric needs rubric "
                        "{checklist,constraints,pass_condition}")
        else:
            # Keys present is not enough: an empty checklist or blank
            # pass_condition gives the judge nothing to anchor on (P1).
            checklist = r.get("checklist")
            has_checklist = bool(checklist) if isinstance(
                checklist, (list, str)) else bool(checklist)
            if not has_checklist:
                errs.append("subjective_rubric checklist is empty "
                            "(needs at least one gradable criterion)")
            if not str(r.get("pass_condition") or "").strip():
                errs.append("subjective_rubric pass_condition is blank")
    elif qtype == "image_ab":
        specs = it.get("image_specs") or {}
        plan = specs.get("flaw_plan") or {}
        if plan:
            errs.extend(validate_flaw_plan(plan))
        else:
            if not (specs.get("image_a_prompt") and specs.get("image_b_prompt")):
                errs.append("image_ab needs image_a_prompt AND image_b_prompt")
            dims = specs.get("dimensions") or {}
            if not isinstance(dims, dict) or not dims:
                errs.append("image_ab needs a non-empty dimensions map")
            else:
                resolved = ab_dims or _ab_fallback_dims()
                by_label = {d["label"].strip().lower(): set(d["choices"])
                            for d in resolved}
                union = set().union(*by_label.values()) if by_label \
                    else set(_AB_CHOICE_SET)
                for label, winner in dims.items():
                    allowed = by_label.get(str(label).strip().lower(), union)
                    if winner not in allowed:
                        errs.append("image_ab dim %r winner %r not in %s"
                                    % (label, winner, sorted(allowed)))
            if not (specs.get("official_reasoning") or "").strip():
                errs.append("image_ab needs official_reasoning")
            # A flaw-free A/B whose EVERY dimension is a tie (both-good/both-bad)
            # has no discriminating ground truth — the 75%-objective verdict path
            # can't grade it. Require at least one decisive (A or B) verdict when
            # there is no planted flaw plan (P1).
            decisive = [
                w for w in (specs.get("dimensions") or {}).values()
                if _is_decisive_ab_winner(w)]
            if isinstance(specs.get("dimensions"), dict) and \
                    specs.get("dimensions") and not decisive:
                errs.append("image_ab has no planted flaw and every dimension is "
                            "a tie (needs at least one decisive A/B winner or a "
                            "flaw_plan)")
    elif qtype == "image_prompt":
        specs = it.get("image_specs") or {}
        imgs = specs.get("images") or []
        if not isinstance(imgs, list) or not any(
                isinstance(i, dict) and i.get("prompt") for i in imgs):
            errs.append("image_prompt needs images[] with a prompt")
        key = specs.get("answer_key") or {}
        if not isinstance(key, dict) or not key.get("ideal_prompt"):
            errs.append("image_prompt needs answer_key with ideal_prompt")
    elif qtype == "video_prompt":
        specs = it.get("image_specs") or {}
        vids = specs.get("videos") or specs.get("images") or []
        if not isinstance(vids, list) or not any(
                isinstance(v, dict) and v.get("prompt") for v in vids):
            errs.append("video_prompt needs videos[] with a prompt")
        key = specs.get("answer_key") or {}
        if not isinstance(key, dict) or not key.get("ideal_prompt"):
            errs.append("video_prompt needs answer_key with ideal_prompt")
    elif qtype == "image_label":
        specs = it.get("image_specs") or {}
        # source_url (real-page DOM capture) is the MANDATORY primary form; a
        # repairable application name counts as a url, since it yields one.
        has_url = bool(str(specs.get("source_url") or "").strip()
                       or _repair_image_label_source_url(specs))
        if not has_url:
            # No source_url and none derivable: only accepted as the SYNTHETIC
            # FALLBACK, which must be COMPLETE on its own (a brief PLUS an answer
            # key/boxes) — an incomplete synthetic-only image_label is rejected
            # so the contract's mandatory-source_url rule keeps its teeth.
            imgs = specs.get("images") or []
            has_brief = isinstance(imgs, list) and any(
                isinstance(i, dict) and i.get("prompt") for i in imgs)
            key = specs.get("answer_key") or {}
            ideal = key.get("ideal_labels") if isinstance(key, dict) else None
            has_map = isinstance(ideal, dict) and any(
                str(v).strip() for v in ideal.values())
            has_str = isinstance(ideal, str) and bool(ideal.strip())
            has_key = has_map or has_str or bool(_image_label_boxes(specs))
            if not has_brief:
                errs.append("image_label needs a source_url to a real public "
                            "page (mandatory real-page capture) or, as fallback, "
                            "an images[] brief")
            elif not has_key:
                errs.append("image_label synthetic fallback needs an "
                            "answer_key.ideal_labels (a per-box map or a string) "
                            "or a boxes[] plan with coordinates")
    else:
        errs.append("unknown question_type %r" % qtype)
    return errs


def _generation_model(env):
    """The document-capable multimodal model used to read a SOP and author
    questions directly; falls back to a document-capable default (NOT the image
    model, which cannot parse a PDF's document structure)."""
    return _param(env, "etp_assessment_pro.generation_model") \
        or GENERATION_DEFAULT_MODEL


def _scoring_model(env):
    """The TEXT model used by the subjective judge. MUST be a text/multimodal
    reasoning model, never the image model: the judge reads the golden answer +
    worker text and returns structured JSON. Resolution order:
    ``scoring_model`` config -> ``generation_model`` config -> the text default.
    The bare ``vertex_model`` default (VERTEX_DEFAULT_MODEL) is deliberately NOT
    consulted here because it is the image model on this deployment, which would
    silently route grading through gemini-3-pro-image (wrong + expensive + image
    quota bound)."""
    return _param(env, "etp_assessment_pro.scoring_model") \
        or _param(env, "etp_assessment_pro.generation_model") \
        or GENERATION_DEFAULT_MODEL


_SOP_MIME_BY_EXT = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "txt": "text/plain", "md": "text/plain", "markdown": "text/plain",
    "csv": "text/plain", "json": "text/plain",
}


def _inline_doc_part(name, data, default_mime="application/pdf"):
    """Build ONE Gemini inlineData part (base64) from a filename + Binary value,
    using the shared mime map and the %PDF header guard. A Binary field returns
    base64 bytes, which is exactly Gemini's inlineData. Returns None when empty.
    Unknown extensions map to ``default_mime`` (pdf for SOP, text/plain for the
    best-effort sample path so docx/unknown files never trip the PDF guard)."""
    import base64
    if not data:
        return None
    ext = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    mime = _SOP_MIME_BY_EXT.get(ext, default_mime)
    raw = data.decode() if isinstance(data, bytes) else data
    if mime == "application/pdf":
        try:
            head = base64.b64decode(raw[:12])
        except Exception:
            head = b""
        if not head.startswith(b"%PDF"):
            raise LLMRefusalError(
                "File %r is not a readable PDF (missing %%PDF header) — "
                "re-upload a valid PDF." % (name or "?"))
    return {"inlineData": {"mimeType": mime, "data": raw}}


_SOP_TEXT_EXTS = frozenset({"docx", "txt", "md", "markdown", "csv",
                            "html", "htm", "json", "xml"})


def _sop_doc_parts(resources):
    """Document parts for the SOP files. PDF and images go NATIVE (inline base64)
    so the model reads real layout/pixels. docx and text formats cannot be sent
    as Gemini inlineData (docx would trip the %PDF guard), so they are extracted
    to text via the resource's own _extract_text and sent as a text part — this
    is what makes a .docx SOP work end to end, not just .pdf."""
    parts = []
    for res in resources.sorted("sequence"):
        ext = (res.name or "").rsplit(".", 1)[-1].lower() if "." in (res.name or "") else ""
        if ext in _SOP_TEXT_EXTS:
            try:
                text, err = res._extract_text()
            except Exception as exc:  # noqa: BLE001
                text, err = "", repr(exc)
            if text and str(text).strip():
                parts.append({"text": "SOP DOCUMENT (%s):\n%s"
                              % (res.name or "sop", text)})
                continue
            _logger.warning("etp_assessment SOP %r extract failed: %s",
                            res.name, err)
        part = _inline_doc_part(res.name or "", res.file)
        if part:
            parts.append(part)
    return parts


def _sample_doc_parts(prompt_record):
    """Native inline part(s) for the optional Sample Questions upload so images
    inside the sample are read too. docx/unknown extensions fall back to
    text/plain (best-effort); pdf/images/txt/md keep working via the mime map."""
    data = getattr(prompt_record, "sample_questions_file", None)
    if not data:
        return []
    name = getattr(prompt_record, "sample_questions_filename", "") or ""
    part = _inline_doc_part(name, data, default_mime="text/plain")
    return [part] if part else []


_TEXT_TYPE_SPEC = {
    "mcq": "options (>=3 strings) and correct_answer (exactly one option)",
    "msq": "options (>=4 strings) and correct_answer (a list of the correct options)",
    "subjective_rubric": "rubric = {checklist, constraints, pass_condition}",
}


_ENVELOPE_REMINDER = (
    " Return ONE JSON OBJECT with three keys, \"metadata\" (the grounded SOP profile: "
    "sop_title, summary, mapping, tags, skills, evidence, required_elements, "
    "covered_by_all, question_spec, gaps), \"questions\" (the array of question "
    "objects, each MAY carry covers_elements), and \"solutions\" (one entry per "
    "question IN THE SAME ORDER, each {answers, rationale} holding the most correct "
    "answer in an ideal worker's voice plus how it is known). First char '{', last "
    "'}', no markdown.")


def _facet_vocabulary_note(env):
    """Build the controlled-vocabulary instruction from the LIVE tag table so the
    model REUSES an existing facet value when one fits, instead of coining a
    synonym that would make two runs of the same SOP look unrelated. The
    vocabulary is data (existing tags), not a hardcoded map: a genuinely novel
    concept is still allowed a NEW kebab-case value, which then joins the list
    for the next SOP. Returns '' when the tag table is empty (cold start), so the
    first extractions simply seed the vocabulary."""
    try:
        vocab = env["etp.assessment.pro.tag"].sudo()._facet_vocabulary()
    except Exception:  # noqa: BLE001 - vocabulary is an optimization, never fatal
        return ""
    if not vocab:
        return ""
    lines = "; ".join(
        "%s: [%s]" % (facet, ", ".join(vals))
        for facet, vals in vocab.items() if vals)
    return (
        " MAPPING VOCABULARY (important for cross-project matching): the platform "
        "already uses these faceted mapping values across existing projects — "
        + lines
        + ". When a facet of THIS SOP means the same thing as one of these, REUSE "
        "the exact existing value (e.g. do not write modality:ui-screenshot if "
        "modality:image already exists for the same idea). Only coin a NEW "
        "kebab-case value when this SOP introduces a genuinely new concept none of "
        "the above covers. Keep each mapping entry as facet:value.")


def _forced_type_directive(force_type, count, ab_dims=None):
    """An EXCLUSIVE directive that pins every generated item to one question_type
    (with that type's required shape), used when the admin forces a type. An image
    type carries its full image_specs contract from _image_question_directive, so
    a run forced to image_ab emits the flaw_plan the platform materializes into a
    ground-truth answer key rather than the legacy a/b-prompt shape."""
    n = count or 5
    if force_type in _IMAGE_OR_VIDEO_TYPES:
        return (f"Generate EXACTLY {n} question(s). EVERY item's question_type MUST "
                f'be exactly "{force_type}" — do NOT produce any other type. '
                + _image_question_directive(force_type, n, ab_dims=ab_dims)
                + _ENVELOPE_REMINDER
                + _SELF_CONTAINED_RULE)
    spec = _TEXT_TYPE_SPEC.get(force_type, "")
    return (f"Generate EXACTLY {n} question(s). EVERY item's question_type MUST be "
            f'exactly "{force_type}" — do NOT produce any other type. For EACH item '
            "provide " + spec + "." + _ENVELOPE_REMINDER
            + _SELF_CONTAINED_RULE)


def _extract_solutions(raw):
    """Pull the top-level "solutions" array from a {metadata, questions, solutions}
    generation response. Returns [] for the legacy bare-array shape or when the
    key is absent/unparseable, so callers can zip defensively by index."""
    try:
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group(0)) if m else None
        if not isinstance(obj, dict):
            return []
        sols = obj.get("solutions")
        if isinstance(sols, dict):
            # keyed by q-id/position -> order by sorted key for stable zip
            return [sols[k] for k in sorted(sols.keys())]
        if isinstance(sols, list):
            return sols
        return []
    except Exception:  # noqa: BLE001
        return []


def _attach_solutions(items, sols, prompt_id):
    """Attach each solution dict to its question as ``it["_solution"]`` WITHOUT
    ever mis-keying it onto the wrong question (P0). Strategy, in order:

    1. REFERENCE MATCH — if solutions carry a "question_ref"/"name"/"id" that
       matches a question's name (case/space-insensitive), pair by that. This
       survives any reordering or a dropped question.
    2. POSITIONAL 1:1 — only when every solution lacks a usable ref AND the two
       arrays are the SAME length, pair by index (the documented same-order
       contract).
    3. SKIP — on a length mismatch with no refs, attach NOTHING and log, so a
       question is graded against NO key rather than the WRONG key.
    """
    if not (items and sols):
        return

    def _norm(v):
        return " ".join(str(v or "").strip().lower().split())

    by_name = {}
    for it in items:
        key = _norm(it.get("name") or it.get("title"))
        if key and key not in by_name:
            by_name[key] = it

    matched = 0
    unref = []
    for sol in sols:
        if not isinstance(sol, dict):
            continue
        ref = _norm(sol.get("question_ref") or sol.get("name")
                    or sol.get("question") or sol.get("id"))
        if ref and ref in by_name:
            by_name[ref]["_solution"] = sol
            matched += 1
        else:
            unref.append(sol)

    if matched:
        # reference matching worked for at least some; do NOT positionally guess
        # the remainder (that is exactly where mis-keying creeps in).
        if unref:
            _logger.warning(
                "etp_assessment (SOP prompt %s): %d solution(s) had no matching "
                "question_ref and were left unattached (kept answer-key integrity "
                "over guessing).", prompt_id, len(unref))
        return

    # No references at all -> positional, but ONLY on an exact 1:1 length match.
    if len(sols) == len(items):
        for it, sol in zip(items, sols):
            if isinstance(sol, dict):
                it["_solution"] = sol
        return

    _logger.warning(
        "etp_assessment (SOP prompt %s): %d solutions vs %d questions with no "
        "question_ref to match on — SKIPPED solution attachment to avoid "
        "mis-keying golden answers to the wrong questions.",
        prompt_id, len(sols), len(items))


def _capture_sop_metadata(env, prompt_record, raw):
    """Persist the FULL research schema-1.5 project profile when the generation
    response is the {metadata, questions} object, and reconcile the faceted
    ``mapping`` into ``tag_ids`` so research's mapping drives our existing
    weighted-Jaccard ranking (parity with the research process). No-op for the
    legacy bare-array shape, so this is safe to call on every run. Best-effort:
    never raises into generation."""
    try:
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group(0)) if m else None
        if not isinstance(obj, dict):
            return
        meta = obj.get("metadata")
        if not isinstance(meta, dict):
            return

        def _dump_list(key):
            v = meta.get(key)
            return json.dumps(v, ensure_ascii=False) if isinstance(v, list) and v else False

        vals = {"metadata_json": json.dumps(meta, ensure_ascii=False)}
        # scalars
        if meta.get("sop_title"):
            vals["sop_title"] = str(meta["sop_title"])[:255]
        if meta.get("summary"):
            vals["sop_summary"] = str(meta["summary"])
        tags = meta.get("tags")
        if isinstance(tags, list) and tags:
            vals["plain_tags"] = ", ".join(str(t) for t in tags)[:255]
        # every list artifact -> its own column (historic project data)
        for meta_key, field_name in (
                ("mapping", "mapping_json"),
                ("skills", "skills_json"),
                ("evidence", "evidence_json"),
                ("required_elements", "required_elements_json"),
                ("covered_by_all", "covered_by_all_json"),
                ("sop_examples", "sop_examples_json"),
                ("quality_criteria", "quality_criteria_json"),
                ("common_failure_modes", "failure_modes_json"),
                ("gaps", "gaps_json"),
                ("conflicts", "conflicts_json"),
                ("injection_flags", "injection_flags_json")):
            dumped = _dump_list(meta_key)
            if dumped:
                vals[field_name] = dumped
        # question_spec is an object, not a list
        qspec = meta.get("question_spec")
        if isinstance(qspec, dict) and qspec:
            vals["question_spec_json"] = json.dumps(qspec, ensure_ascii=False)

        # RANKING PARITY: reconcile faceted mapping -> tag_ids. Research's mapping
        # is exactly our prefixed-tag form (facet:value), so feed it straight into
        # the same tag vocabulary the weighted-Jaccard ranking already reads.
        mapping = meta.get("mapping")
        if isinstance(mapping, list) and mapping:
            tag_model = env["etp.assessment.pro.tag"].sudo()
            tag_recs = tag_model._get_or_create(
                [str(m) for m in mapping if str(m).strip()])
            if tag_recs:
                vals["tag_ids"] = [(6, 0, tag_recs.ids)]

        prompt_record.sudo().write(vals)
        _logger.info(
            "etp_assessment captured research metadata for prompt %s "
            "(%d evidence, %d required_elements, %d mapping->tags, %d skills)",
            prompt_record.id,
            len(meta.get("evidence") or []),
            len(meta.get("required_elements") or []),
            len(meta.get("mapping") or []),
            len(meta.get("skills") or []))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("etp_assessment metadata capture skipped: %s",
                        repr(exc)[:160])
def _text_contracts_note(types):
    """The answer-key shapes for the TEXT types in an allow-list — the text twin of
    _image_contracts_note, so a MIXED text+image set gets BOTH halves of its
    contract. Empty when the allow-list has no text types."""
    wanted = [qt for qt in _QUESTION_TYPE_ORDER
              if qt in _TEXT_TYPE_SPEC and qt in types]
    if not wanted:
        return ""
    contracts = " ".join(
        "For %s items provide %s." % (qt, _TEXT_TYPE_SPEC[qt]) for qt in wanted)
    return ("\n\nTEXT-TYPE OUTPUT CONTRACTS (mandatory for each text item you "
            "author): " + contracts)


def _allowed_types_directive(allowed, count, ab_dims=None):
    """EXCLUSIVE directive for an allow-list run: every item's question_type must be
    one of ``allowed`` (an ordered, deduped, already-validated tuple of >=1 codes),
    with the output contract for EVERY allowed type merged in. A single-element
    list returns _forced_type_directive's exact PROMPT TEXT (the emitted directive
    is bit-identical to the old forced run); note the applier still differs — it
    FILTERS out-of-list items rather than OVERRIDING their type (see
    _resolve_item_type). A mixed text+image list emits BOTH the text and image
    contracts and lets the model pick the best type per item; any item whose type
    is outside the list is dropped downstream by _resolve_item_type."""
    if len(allowed) == 1:
        return _forced_type_directive(allowed[0], count, ab_dims=ab_dims)
    type_list = ", ".join('"%s"' % t for t in allowed)
    count_clause = f"Generate approximately {count} question(s). " if count else ""
    return (
        count_clause
        + "EVERY item's question_type MUST be one of [" + type_list + "] — do "
        "NOT produce any other type; an item of any other type is DISCARDED. "
        "Choose the best-fitting allowed type per item, and use EVERY allowed "
        "type at least once across the batch where the SOP supports it. "
        "Return ONLY a JSON array, no markdown."
        + _text_contracts_note(allowed)
        + _image_contracts_note(ab_dims, types=allowed)
        + _SELF_CONTAINED_RULE)


def _resolve_item_type(item, allowed):
    """The question_type to persist for one generated item, or None to DROP it.

    No allow-list -> today's behaviour (unknown/missing type defaults to mcq).
    With an allow-list the list is a FILTER, not an override: an out-of-list item
    is DROPPED, never restamped — its payload is shaped for the type the model
    chose, so restamping either fails _validate_question_item (image types) or,
    worse, passes with the wrong answer-key shape (an mcq payload stamped msq). A
    single-type allow-list keeps the old convenience of stamping an item whose
    type is missing/garbled, since the intent is unambiguous.

    NOTE: this matches the old single-forced behaviour for the common case, but it
    FILTERS rather than OVERRIDES — an item the model authored as a different but
    valid type is dropped (not relabelled), which the old forced path would have
    stamped-then-usually-dropped-as-malformed. Same net persistence, cleaner log."""
    raw = item.get("question_type")
    # isinstance guard: a model may return a non-string question_type (e.g. a list
    # ["mcq"]); `x in <frozenset>` would raise TypeError: unhashable and abort the
    # WHOLE batch. Treat any non-string as "no usable type" and let the rules below
    # drop or default it, keeping the batch's other valid items.
    qtype = raw if isinstance(raw, str) and raw in _QUESTION_TYPES else None
    if not allowed:
        return qtype or "mcq"
    if qtype is None and len(allowed) == 1:
        return allowed[0]
    return qtype if qtype in allowed else None


def generate_questions_from_sop(env, prompt_record, sample_text="", count=0,
                                allowed_types=()):
    """SKILL-FREE generation: send the SOP document(s) NATIVELY (with their
    images/layout) plus an optional sample-question format, and let the best
    multimodal model author questions directly following the format IN the SOP.
    When ``allowed_types`` is a non-empty tuple of valid codes, every item is
    restricted to those types (the model still picks the best fit per item; a
    single-element tuple reproduces the old forced-type behaviour). Reuses the
    same parse / validate / draft-creation pipeline, minus the per-skill
    machinery."""
    doc_parts = _sop_doc_parts(prompt_record.resource_ids)
    sample_parts = _sample_doc_parts(prompt_record)
    has_sample = bool(sample_parts or sample_text)
    notes = (prompt_record.source_text or "").strip()
    if not doc_parts and not notes:
        raise LLMRefusalError(
            "No SOP document or notes to generate from — upload a SOP file first.")
    system_prompt = _get_question_prompt(env)
    model = _generation_model(env)
    allowed = tuple(dict.fromkeys(allowed_types or ()))
    # A valid entry must be a known code string. Non-strings (e.g. None/False from a
    # blank Selection) count as unknown so the gate still fails CLOSED with a clear
    # ValueError — repr() keeps sorted()/join() from choking on a None entry.
    unknown = [t for t in allowed
               if not (isinstance(t, str) and t in _QUESTION_TYPES)]
    if unknown:
        # Fail CLOSED: an unknown code means constants and the DB disagree (a type
        # retired without a migration). The cron writes this to sop_gen_error and
        # the form surfaces it — far better than silently dropping the allow-list.
        raise ValueError(
            "Unknown question type(s) in the generation allow-list: %s"
            % ", ".join(sorted(map(repr, unknown))))
    ab_dims = _ab_fallback_dims()
    vocab_note = _facet_vocabulary_note(env)
    if allowed:
        directive = (
            _allowed_types_directive(allowed, count, ab_dims=ab_dims)
            + vocab_note)
    else:
        directive = (
        "The attached document is a SOP that contains the test content, any "
        "images, AND the required question format. Author assessment questions "
        "that FOLLOW THE FORMAT shown in the SOP"
        + (" and in the attached SAMPLE QUESTIONS" if has_sample else "")
        + ". "
        + (f"Generate approximately {count} question(s). " if count else "")
        + "Each item's question_type must be one of " + QUESTION_TYPE_PROMPT_LIST
        + ". QUESTION-TYPE POLICY (mandatory): if the SOP is about visual or image "
          "evaluation (judging, comparing, ranking, rating, or generating images), "
          "then image_ab, image_prompt and image_label MUST be the MAJORITY of the "
          "questions you "
          "author (at least half) — the candidate must be tested on ACTUAL images, "
          "not on prose that describes them. Do NOT replace an image question with "
          "an mcq/msq/subjective item that merely describes or refers to images. "
          "Use text types only for the genuinely text-based parts of the SOP "
          "(definitions, rules, procedures needing no visual judgment). "
        + _ENVELOPE_REMINDER
        + _image_contracts_note(ab_dims)
        + _SELF_CONTAINED_RULE
        + vocab_note)
    user_parts = list(doc_parts)
    if notes:
        user_parts.append({"text": "ADDITIONAL NOTES:\n" + notes})
    if sample_parts:
        user_parts.append(
            {"text": "SAMPLE QUESTIONS (match this format) — see the attached "
                     "sample document:"})
        user_parts.extend(sample_parts)
    elif sample_text:
        user_parts.append(
            {"text": "SAMPLE QUESTIONS (match this format):\n" + sample_text})
    user_parts.append({"text": directive})

    def _run(parts, note):
        raw = _call_vertex(
            env, system_prompt, user_text="", user_parts=parts, model=model,
            max_tokens=_GEN_MAX_OUTPUT_TOKENS, temperature=0.5, response_json=True,
            usage_ctx={"operation": "generate_questions",
                       "prompt_id": prompt_record.id, "note": note})
        # Research schema 1.5 returns ONE object {metadata, questions}; capture
        # the grounded metadata block (evidence, mapping, tags, skills,
        # required_elements, question_spec) when present, then hand the questions
        # list to the existing parser. Legacy bare-array output is unaffected:
        # _extract_json_array + _unwrap_json_list already yield the same list.
        _capture_sop_metadata(env, prompt_record, raw)
        items = [it for it in _extract_json_array(raw) if isinstance(it, dict)]
        # Research schema 1.5 also returns a top-level "solutions" array, one
        # entry per question. Attach each solution to its question. PREFER an
        # explicit reference (a solution's "question_ref"/"name"/"id" matching a
        # question's name) so the answer key can NEVER land on the wrong question;
        # fall back to positional zip ONLY when the two arrays are the same length
        # (a safe 1:1). On a length mismatch with no usable refs we SKIP the zip
        # entirely and log, rather than silently mis-key golden answers onto the
        # wrong questions (P0: a mis-keyed solution grades a worker against another
        # question's answer).
        sols = _extract_solutions(raw)
        if sols:
            _attach_solutions(items, sols, prompt_record.id)
        return items

    try:
        items = _run(user_parts, prompt_record.name or "SOP")
    except ValueError:
        items = []
    if not items:
        # even salvage recovered nothing (badly truncated/garbled JSON); re-roll
        # ONCE with an explicit terse-output directive so the whole array fits
        # under the token ceiling, instead of hard-failing the whole generation
        _logger.warning(
            "SOP generation for prompt %s produced no parseable items; retrying "
            "once with a terse-output directive.", prompt_record.id)
        try:
            items = _run(user_parts + [{"text": _TERSE_RETRY_DIRECTIVE}],
                         "%s (retry)" % (prompt_record.name or "SOP"))
        except ValueError:
            items = []
    PromptQuestion = env["etp.assessment.pro.prompt.question"].sudo()
    draft_ids = []
    dropped_out_of_scope = 0
    for it in items:
        name = (it.get("name") or it.get("title") or "").strip()
        prompt_text = (it.get("prompt") or "").strip()
        if not name and not prompt_text:
            continue
        qtype = _resolve_item_type(it, allowed)
        if qtype is None:
            dropped_out_of_scope += 1
            _logger.warning(
                "etp_assessment (SOP) dropped out-of-scope %r item; allow-list=%s",
                it.get("question_type"), ",".join(allowed))
            continue
        if _item_cites_source(it):
            _logger.warning(
                "etp_assessment (SOP) dropped source-citing %s item", qtype)
            continue
        violations = _validate_question_item(it, qtype, ab_dims=ab_dims)
        if violations:
            _logger.warning(
                "etp_assessment (SOP) skipped malformed %s item: %s",
                qtype, "; ".join(violations))
            continue
        difficulty = it.get("difficulty") if it.get("difficulty") in _DIFFICULTIES \
            else "medium"
        vals = {
            "prompt_id": prompt_record.id,
            "name": (name or prompt_text[:60])[:200],
            "question_prompt": prompt_text or name,
            "question_type": qtype,
            "difficulty": difficulty,
            # Candidate-facing description: use the model's when given, else leave
            # blank (it's optional). Never silently duplicate the prompt.
            "description": (it.get("description") or "").strip() or False,
        }
        ce = it.get("covers_elements")
        if isinstance(ce, list) and ce:
            vals["covers_elements_json"] = json.dumps(ce, ensure_ascii=False)
        # Research schema 1.5 solution (golden answer + rationale) zipped on by
        # _run: store as historic ground truth + the subjective judge's key.
        sol = it.get("_solution")
        if isinstance(sol, dict):
            ans = sol.get("answers")
            if ans is not None:
                vals["solution_json"] = json.dumps(ans, ensure_ascii=False)
            if sol.get("rationale"):
                vals["solution_rationale"] = str(sol["rationale"])
        if qtype in _IMAGE_OR_VIDEO_TYPES:
            img_fields = _build_image_draft_fields(
                env, qtype, it, ab_dims=ab_dims,
                usage_ctx={"operation": "generate_image",
                           "prompt_id": prompt_record.id,
                           "note": prompt_record.name or "SOP"})
            # image_ab: override the golden solution VERDICTS with the ones
            # derived from the authoritative construction_keys (slot-aligned,
            # swap-corrected), so the stored answer key always matches the
            # rendered images. Keep the model's justification/rationale prose.
            derived = img_fields.pop("_derived_ab_solution", None)
            if isinstance(derived, dict) and derived:
                model_ans = it.get("_solution") or {}
                model_ans = model_ans.get("answers") if isinstance(
                    model_ans, dict) else {}
                justification = (model_ans.get("justification")
                                 if isinstance(model_ans, dict) else "") or ""
                merged = dict(derived)
                if justification:
                    merged["justification"] = justification
                vals["solution_json"] = json.dumps(merged, ensure_ascii=False)
            vals.update(img_fields)
            if vals.get("image_brief_json") or vals.get("video_brief_json"):
                vals["image_state"] = "pending"
            if vals.get("video_brief_json"):
                vals["video_state"] = "pending"
        else:
            options = it.get("options") or []
            vals.update({
                "options_json": json.dumps(options, ensure_ascii=False)
                if options else False,
                "correct_answer_json": json.dumps(
                    it.get("correct_answer"), ensure_ascii=False)
                if it.get("correct_answer") is not None else False,
                "rubric_json": json.dumps(it.get("rubric"), ensure_ascii=False)
                if it.get("rubric") else False,
                "official_reasoning": it.get("official_reasoning") or False,
            })
        draft_ids.append(PromptQuestion.create(vals).id)
    _logger.info(
        "etp_assessment generated %s drafts from SOP on prompt=%s "
        "(%s dropped as out-of-scope for the allow-list)",
        len(draft_ids), prompt_record.id, dropped_out_of_scope)
    return draft_ids


_TAG_SYSTEM_PROMPT = (
    "You are a taxonomy expert. Read the attached SOP and output ONLY a JSON "
    "array of 4-8 SHORT, DISTINCTIVE, NON-REDUNDANT semantic tags that "
    "characterize the ASSESSMENT TASK the SOP defines. Rules: (1) every tag is "
    "lowercase kebab-case with exactly one prefix from domain:/task:/skill:/"
    "modality:/output-format: (e.g. \"task:pairwise-comparison\", "
    "\"domain:image-evaluation\", \"skill:prompt-deconstruction\", "
    "\"modality:image\", \"output-format:bounding-box\"). (2) Keep the set "
    "SMALL and DISTINCTIVE — tags that would apply to almost ANY SOP (generic "
    "filler like task:evaluation, skill:reading, domain:assessment) are "
    "FORBIDDEN. (3) NO duplicates and no two tags meaning the same thing. "
    "(4) Prefer a specific label over a vague one. (5) CROSS-PROJECT "
    "CONSISTENCY IS CRITICAL: when an EXISTING TAG VOCABULARY is provided below, "
    "you MUST reuse the existing value for any facet whose meaning matches — a UI "
    "screenshot is modality:image (NOT modality:ui-screenshot or "
    "modality:application-screenshot); labelling/annotating is task:annotation "
    "(NOT task:image-annotation or task:labeling); a labels output is "
    "output-format:labels (NOT output-format:text-labels). Coin a NEW value "
    "ONLY for a concept the vocabulary genuinely lacks. Two runs on the same SOP "
    "must produce the SAME tags. Output nothing but the JSON array.\n\n"
    "Example — an image A/B comparison SOP -> "
    '["domain:image-evaluation","task:pairwise-comparison",'
    '"skill:prompt-deconstruction","skill:defect-detection","modality:image"]. '
    "Example — a bounding-box UI labelling SOP -> "
    '["domain:ui-ux","task:annotation",'
    '"skill:ui-element-identification","skill:functional-description",'
    '"modality:image","output-format:labels"].'
)


def extract_tags_from_sop(env, prompt_record):
    """Extract 4-8 short, prefixed, non-redundant semantic tags characterizing
    the SOP's task. Sends the SOP document(s) NATIVELY to the multimodal model
    and injects the EXISTING tag vocabulary so the model reuses a tag verbatim
    when one fits instead of drifting to a near-duplicate. Returns
    ``(list_of_tag_strings, raw_json_text)``; never raises on an empty parse
    (returns ``([], raw)``) so a barren SOP just yields no tags. Canonicalization
    happens in etp.assessment.pro.tag._get_or_create."""
    doc_parts = _sop_doc_parts(prompt_record.resource_ids)
    notes = (prompt_record.source_text or "").strip()
    if not doc_parts and not notes:
        raise LLMRefusalError(
            "No SOP document or notes to tag — upload a SOP file first.")
    directive = (
        "Extract the semantic tags for THIS SOP now, following every rule. "
        "Return ONLY the JSON array.")
    vocab = env["etp.assessment.pro.tag"].sudo()._facet_vocabulary()
    if vocab:
        grouped = "\n".join(
            "  %s: %s" % (facet, ", ".join(vals))
            for facet, vals in vocab.items() if vals)
        directive += (
            "\n\nEXISTING TAG VOCABULARY, grouped by facet (reuse a value from "
            "this list VERBATIM when it means the same thing for this SOP — e.g. "
            "do not write modality:ui-screenshot if modality:image already "
            "covers the idea; only invent a NEW kebab-case value when this SOP "
            "introduces a concept none of these cover):\n" + grouped)
    user_parts = list(doc_parts)
    if notes:
        user_parts.append({"text": "ADDITIONAL NOTES:\n" + notes})
    user_parts.append({"text": directive})
    raw = _call_vertex(
        env, _TAG_SYSTEM_PROMPT, user_text="", user_parts=user_parts,
        model=_generation_model(env), max_tokens=4096, temperature=0.2,
        response_json=True,
        usage_ctx={"operation": "extract_tags",
                   "prompt_id": prompt_record.id,
                   "note": prompt_record.name or "SOP"})
    try:
        parsed = _extract_json_array(raw)
    except ValueError:
        return [], raw
    names = [str(t).strip() for t in parsed
             if isinstance(t, (str, int, float)) and str(t).strip()]
    return names, raw
