import json
import logging
import os
import re
import time

from ..constants import (
    QUESTION_TYPE_CODES as _QUESTION_TYPES,
    DIFFICULTY_CODES as _DIFFICULTIES,
    IMAGE_QUESTION_TYPES as _IMAGE_TYPES,
    MEDIUM_CODES as _MEDIA,
    AB_DIMENSION_NAMES as _AB_DIM_NAMES,
    AB_CHOICES as _AB_CHOICES,
    AB_CHOICE_SET as _AB_CHOICE_SET,
    QUESTION_TYPE_PROMPT_LIST,
    VERTEX_DEFAULT_LOCATION,
    VERTEX_DEFAULT_MODEL,
    VERTEX_GLOBAL_LOCATION,
)

_logger = logging.getLogger(__name__)

INLINE_SKILL_GEN_PROMPT = (
    "You are an expert assessment designer. Read the provided SOP / vendor / "
    "client documents and extract a JSON array of distinct skills a candidate "
    "must be tested on. Each item must be a JSON object with keys: "
    'name (string, short), description (string), tags (string, comma-separated), '
    'medium (one of text/image - the source medium the skill is about), '
    'question_type (one of ' + QUESTION_TYPE_PROMPT_LIST + '), '
    'question_count (integer 3-10), '
    'time_minutes (integer 5-30), difficulty (easy/medium/hard). '
    "Use image_ab or image_text ONLY when medium is image. Return ONLY a "
    "JSON array, no markdown."
)

INLINE_QUESTION_PROMPT = (
    "You are an expert assessment author. Generate questions for the given "
    "SKILL grounded in the supplied artifacts. Return ONLY a JSON array, no "
    "markdown. Each item: name (short title), prompt (full question text), "
    'question_type (one of ' + QUESTION_TYPE_PROMPT_LIST + '), '
    'difficulty (easy/medium/hard), and the answer-key '
    "fields its type needs: mcq -> options (list) + correct_answer (string); "
    "msq -> options (list) + correct_answer (list); subjective_rubric -> "
    "rubric (object with checklist/constraints/pass_condition); "
    "subjective_justification -> no rubric (graded on the prompt). For the "
    "image types the per-request directive gives the exact image_specs shape; "
    "do NOT emit options/correct_answer for image types."
)


def _param(env, key, default=""):
    val = env["ir.config_parameter"].sudo().get_param(key, default) or default
    if isinstance(val, str) and "PLACEHOLDER" in val:
        return default
    return val


def _vertex_creds(env):
    return (
        _param(env, "etp_assessment_pro.vertex_project_id"),
        # Gemini-3 series is served on the ``global`` endpoint.
        _param(env, "etp_assessment_pro.vertex_location", VERTEX_DEFAULT_LOCATION),
        _param(env, "etp_assessment_pro.vertex_model", VERTEX_DEFAULT_MODEL),
        _param(env, "etp_assessment_pro.vertex_api_key"),
    )


def _vertex_image_model(env):
    """The single configured model (image rendering is unified with every other task)."""
    return _vertex_creds(env)[2]


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


def _get_skill_gen_prompt(env):
    p = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment_pro.skill_gen_prompt", "") or "").strip()
    if p:
        return p
    bundled = _load_bundled_prompt("skill_gen.md")
    return bundled.strip() if bundled.strip() else INLINE_SKILL_GEN_PROMPT


def _get_question_prompt(env):
    p = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment_pro.question_prompt", "") or "").strip()
    if p:
        return p
    bundled = _load_bundled_prompt("question.md")
    return bundled.strip() if bundled.strip() else INLINE_QUESTION_PROMPT


# Best-effort USD rates per model (in/out per 1M tokens + per image); cost_usd
# is a budgeting estimate, tunable without touching the ledger.
_PRICING = {
    "gemini-3.1-pro-preview": {"in": 2.00, "out": 12.00, "image": 0.0},
    "gemini-3-pro-image":     {"in": 2.00, "out": 12.00, "image": 0.134},
    "gemini-2.5-flash-image": {"in": 0.30, "out": 2.50,  "image": 0.039},
    "gemini-2.5-flash":       {"in": 0.30, "out": 2.50,  "image": 0.0},
    "gemini-2.5-flash-lite":  {"in": 0.10, "out": 0.40,  "image": 0.0},
    "gemini-2.5-pro":         {"in": 1.25, "out": 10.00, "image": 0.0},
}
_DEFAULT_PRICE = {"in": 1.0, "out": 5.0, "image": 0.0}


def _estimate_cost(model, tokens_in, tokens_out, thoughts, image_count):
    p = _PRICING.get(model or "", _DEFAULT_PRICE)
    out_tok = (tokens_out or 0) + (thoughts or 0)  # thinking is billed as output
    return (((tokens_in or 0) * p["in"] + out_tok * p["out"]) / 1_000_000.0
            + (image_count or 0) * p["image"])


def _log_usage(env, model, usage_meta, image_count, ctx):
    """Write one LLM-usage ledger row. Best-effort: failures are swallowed."""
    try:
        meta = usage_meta or {}
        ctx = ctx or {}
        ti = int(meta.get("promptTokenCount") or 0)
        to = int(meta.get("candidatesTokenCount") or 0)
        th = int(meta.get("thoughtsTokenCount") or 0)
        env["etp.assessment.pro.llm.usage"].sudo().create({
            "operation": ctx.get("operation") or "other",
            "model": model or "",
            "tokens_in": ti,
            "tokens_out": to,
            "thoughts_tokens": th,
            "image_count": image_count or 0,
            "cost_usd": _estimate_cost(model, ti, to, th, image_count),
            "prompt_id": ctx.get("prompt_id") or False,
            "skill_id": ctx.get("skill_id") or False,
            "evaluator_id": ctx.get("evaluator_id") or False,
            "note": (ctx.get("note") or "")[:120],
        })
        # Per-call ledger line so every LLM call's cost/tokens are visible in the
        # console (the module runs with logfile off). This is the one chokepoint
        # every text AND image call passes through.
        _logger.info(
            "etp_assessment LLM usage: op=%s model=%s in=%d out=%d thoughts=%d "
            "images=%d cost=$%.4f note=%s",
            ctx.get("operation") or "other", model or "", ti, to, th,
            image_count or 0, _estimate_cost(model, ti, to, th, image_count),
            (ctx.get("note") or "")[:60])
    except Exception:
        _logger.exception("etp_assessment: LLM usage log failed")


class LLMRefusalError(RuntimeError):
    """The model declined / was blocked / returned no usable text.

    Distinct from transport/parse errors so callers can give an actionable message.
    """


# finishReason values meaning "no usable answer"; STOP/MAX_TOKENS still carry text.
_BLOCKING_FINISH_REASONS = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "OTHER",
}
# Prose openings a model uses when it refuses instead of emitting JSON.
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
    if t[:1] in ("[", "{"):  # already JSON -> not a refusal
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


# Hard ceiling for the MAX_TOKENS auto-retry so a think-loop can't escalate unbounded.
_MAX_OUTPUT_TOKENS_CEILING = 64000

# Starting output budget for JSON generation calls; must hold hidden thinking
# tokens AND the JSON, since Gemini-3 ignores thinkingBudget=0. A ceiling, not a
# charge; the doubling retry can still escalate to _MAX_OUTPUT_TOKENS_CEILING.
_GEN_MAX_OUTPUT_TOKENS = 32000


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
                 response_schema=None):
    """Single text generateContent call with a built-in MAX_TOKENS recovery.

    Heavy hidden 'thinking' can eat the whole budget (finishReason=MAX_TOKENS,
    no parts); we retry once with a doubled budget (<= _MAX_OUTPUT_TOKENS_CEILING).
    """
    import httpx
    _project, _loc, model, _key = _vertex_creds(env)
    url, headers = _gemini_request(env, model, "generateContent")

    attempt_tokens = max_tokens
    last_finish = None
    for attempt in range(2):
        gen_config = {
            "maxOutputTokens": attempt_tokens,
            "temperature": temperature,
        }
        _apply_thinking_budget(gen_config, model)
        # Force valid-JSON output so a prose refusal can't crash the parser.
        # (gemini-3-pro-image honours responseMimeType for its answer parts; its
        # reasoning comes back separately as thought parts, which we skip.)
        if response_json:
            gen_config["responseMimeType"] = "application/json"
            if response_schema:
                gen_config["responseSchema"] = response_schema
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": gen_config,
        }
        _logger.info(
            "etp_assessment Vertex call: model=%s max_tokens=%d json=%s attempt=%d",
            model, attempt_tokens, response_json, attempt + 1,
        )
        with httpx.Client(
            timeout=httpx.Timeout(connect=30, read=180, write=60, pool=30)
        ) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Vertex error [{resp.status_code}]: {resp.text[:400]}"
            )
        data = resp.json()
        _log_usage(env, model, data.get("usageMetadata"), 0, usage_ctx)
        # Prompt-level block (whole request rejected before any candidate).
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
        # Gemini IMAGE models (used here as the single model) split a long text
        # answer across MANY text parts; reading only parts[0] truncated the JSON
        # mid-stream. Concatenate every text part, skipping hidden 'thought'
        # parts. No-op for normal text models, which return a single part.
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(
            p["text"] for p in parts
            if isinstance(p, dict) and p.get("text") and not p.get("thought"))
        if not text:
            # MAX_TOKENS with no parts == budget went to hidden thinking; retry bigger.
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
        # MAX_TOKENS with partial text == truncated mid-stream; retry bigger
        # rather than hand the caller half a JSON array.
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
        # Truncated/garbled JSON under finishReason=STOP slips past the retries
        # above; detect it and retry bigger so heavy-thinking runs self-heal.
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


def generate_image(env, image_prompt, *, aspect_hint=None, usage_ctx=None):
    """Text->image via the Gemini image model. Returns ``(b64_str, mime)``;
    raises RuntimeError when no image part comes back (e.g. a safety block)."""
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
    with httpx.Client(
        timeout=httpx.Timeout(connect=30, read=180, write=60, pool=30)
    ) as client:
        resp = client.post(url, json=payload, headers=headers)
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


def _extract_json_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return _unwrap_json_list(json.loads(text))
    except Exception:
        pass
    # Try a bare array first, then an object we can unwrap to its inner array.
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                out = _unwrap_json_list(json.loads(m.group(0)))
                if isinstance(out, list):
                    return out
            except Exception:
                pass
    raise ValueError(
        "Could not parse JSON array from LLM response: %s" % text[:200]
    )


# A text->image prompt is a detailed, self-contained brief (every deciding
# detail spelled out, on-image text quoted exactly) for evidence-grade images.
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


def _resolve_ab_dimensions(skill):
    """A/B axes + verdicts from the skill's ``image_ab_dimension_ids``, else the constant rubric."""
    if skill and skill.image_ab_dimension_ids:
        dims = []
        for d in skill.image_ab_dimension_ids:
            choices = [o.name for o in d.option_ids if o.name]
            if d.name and choices:
                dims.append({"label": d.name, "choices": choices})
        if dims:
            return dims
    return _ab_fallback_dims()


def _image_question_directive(qtype, count, ab_dims=None):
    """Directive appended to the question prompt for renderable image briefs + answer key."""
    base = (
        f"Generate exactly {count} question(s) of type '{qtype}' as one JSON "
        "array. Do NOT emit mcq-style \"options\"/\"correct_answer\". Every "
        "item MUST contain a non-empty \"image_specs\" object. " + _IMG_PROMPT_RULE + " "
    )
    if qtype == "image_ab":
        dims = ab_dims or _ab_fallback_dims()
        axes_lines = "\n".join(
            "  - %s: one of [%s]" % (d["label"], ", ".join(d["choices"]))
            for d in dims)
        ex_label = dims[0]["label"] if dims else "Overall Choice"
        ex_choice = (dims[0]["choices"][0]
                     if dims and dims[0]["choices"] else "Response A")
        return base + (
            'Shape: {"name": "...", "question_type": "image_ab", "prompt": '
            '"...", "difficulty": "medium", "image_specs": {"image_a_prompt": '
            '"detailed self-contained brief for Response A", "image_b_prompt": '
            '"detailed self-contained brief for Response B", "dimensions": '
            '{"%s": "%s"}, "official_reasoning": "why those ratings are '
            'correct"}}. ' % (ex_label, ex_choice)
            + "Score EXACTLY these dimensions, using each label verbatim as a "
            "key of \"dimensions\", and pick ONE verdict from that axis's "
            "allowed list:\n" + axes_lines + "\n"
            "Do NOT invent other dimensions and do NOT use a verdict outside a "
            "dimension's allowed list. image_a_prompt and image_b_prompt are "
            "REQUIRED. CRITICAL: A and B MUST differ in the ONE deciding detail "
            "AND in at least two incidental dimensions (framing, palette, or "
            "background) so two renders cannot come out near identical. The "
            "deciding detail must be VISIBLE in the rendered image, never only "
            "in the prompt text, and must NOT be a label that spells out the "
            "answer. Do not depend on text being unreadable or on a tiny "
            "pixel-exact difference the renderer cannot guarantee."
        )
    return base + (
        'Shape: {"name": "...", "question_type": "image_text", "prompt": '
        '"...", "difficulty": "medium", "image_specs": {"images": [{"slot": '
        '"single", "label": "Image", "prompt": "detailed self-contained brief '
        'for the stimulus"}], "answer_key": {"ideal_answer": "...", '
        '"mandatory_elements": ["..."], "penalty_rules": ["..."], '
        '"scoring_guide": "..."}}}. Emit EXACTLY ONE image with slot "single": '
        "this is a one-image prompt-writing / labelling question where the "
        "candidate writes a text answer graded against the answer_key. The "
        'image "prompt" is REQUIRED. The stimulus must SHOW the evidence the '
        "question is about, never a caption that states the answer."
    )


def _image_brief(scene):
    """Wrap a scene description in the strict render brief (single source of truth)."""
    return (
        "Generate exactly one image and no other text: a clean, photorealistic "
        "image. It must show every detail this brief states; the brief is the "
        "single source of truth, follow it alone and treat it as the only "
        "attempt at the scene. Render any quoted text, labels, and numbers "
        "exactly and legibly:\n" + (scene or "")
    )


def _build_image_draft_fields(env, qtype, item, usage_ctx=None, ab_dims=None):
    """Build the answer-key + image-brief fields for one image draft (no rendering here)."""
    specs = item.get("image_specs") or {}
    vals = {}
    briefs = []

    if qtype == "image_ab":
        if specs.get("image_a_prompt"):
            briefs.append({"slot": "a", "label": "Response A",
                           "prompt": specs["image_a_prompt"]})
        if specs.get("image_b_prompt"):
            briefs.append({"slot": "b", "label": "Response B",
                           "prompt": specs["image_b_prompt"]})
        # Per-axis options come from the resolved dimension set, matched to the
        # model's label; an unrecognised label falls back to the union of verdicts.
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
    else:  # image_text
        # One-image question: keep a single stimulus even if the model emitted more.
        for img in (specs.get("images") or []):
            if img.get("prompt"):
                briefs.append({
                    "slot": "single",
                    "label": img.get("label") or "Image",
                    "prompt": img["prompt"],
                })
                break
        answer_key = specs.get("answer_key") or {}
        if answer_key:
            vals["rubric_json"] = json.dumps(answer_key, ensure_ascii=False)

    if briefs:
        vals["image_brief_json"] = json.dumps(briefs, ensure_ascii=False)
    return vals


def render_draft_images(env, briefs, usage_ctx=None, only_slot=None):
    """Render briefs -> ``[{slot,label,data}]``; per-brief failures are skipped.

    On-demand Model 2 step, never run from the generate-questions request.
    ``only_slot`` (when set) renders just that slot.
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
        except Exception as exc:
            _logger.warning(
                "etp_assessment image render failed (%s): %s",
                slot, repr(exc)[:160])
    return images


def extract_skills(env, prompt_record):
    source_text = prompt_record._compiled_source_text()
    system_prompt = _get_skill_gen_prompt(env)
    raw = _call_vertex(
        env, system_prompt, source_text, max_tokens=_GEN_MAX_OUTPUT_TOKENS,
        temperature=0.3,
        response_json=True,
        usage_ctx={"operation": "extract_skills",
                   "prompt_id": prompt_record.id,
                   "note": prompt_record.name},
    )
    items = _extract_json_array(raw)
    Skill = env["etp.assessment.pro.skill"].sudo()
    PromptSkill = env["etp.assessment.pro.prompt.skill"].sudo()
    created = 0
    skipped = 0
    bank_ids = []
    seq = 10
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qtype = it.get("question_type") if it.get("question_type") in _QUESTION_TYPES else "mcq"
        medium = it.get("medium") if it.get("medium") in _MEDIA else "text"
        # Keep medium and question_type COHERENT — the skill model constrains them
        # and a violation aborts the WHOLE extraction. The model sometimes pairs
        # them wrong both ways, so normalize both directions:
        #  - an image question type needs the image medium; if the model gave a
        #    non-image medium, downgrade the type to a subjective text type.
        #  - a non-image (text) question type cannot carry the image medium; if the
        #    model gave medium=image with a text type, set the medium to text.
        if qtype in _IMAGE_TYPES and medium != "image":
            qtype = "subjective_rubric"
        elif qtype not in _IMAGE_TYPES and medium == "image":
            medium = "text"
        difficulty = it.get("difficulty") if it.get("difficulty") in _DIFFICULTIES else "medium"
        try:
            qcount = max(1, int(it.get("question_count") or 5))
        except (TypeError, ValueError):
            qcount = 5
        try:
            time_min = max(1, int(it.get("time_minutes") or 10))
        except (TypeError, ValueError):
            time_min = 10
        existing = Skill.search([("name", "=", name)], limit=1)
        if existing:
            skipped += 1
            bank_skill = existing
            upsert_state = "skipped"
        else:
            bank_skill = Skill.create({
                "name": name,
                "description": it.get("description") or False,
                "tags": it.get("tags") or False,
                "question_type": qtype,
                "medium": medium,
                "question_count": qcount,
                "time_minutes": time_min,
                "difficulty": difficulty,
                "extracted_from_prompt_id": prompt_record.id,
                "source_resource_ids": [
                    (6, 0, prompt_record.resource_ids.ids)
                ] if prompt_record.resource_ids else False,
            })
            created += 1
            upsert_state = "created"
        bank_ids.append(bank_skill.id)
        PromptSkill.create({
            "prompt_id": prompt_record.id,
            "name": name,
            "description": it.get("description") or False,
            "tags": it.get("tags") or False,
            "sequence": seq,
            "question_type": qtype,
            "question_count": qcount,
            "time_minutes": time_min,
            "difficulty": difficulty,
            "bank_skill_id": bank_skill.id,
            "upsert_state": upsert_state,
        })
        seq += 10
    if bank_ids:
        prompt_record.write({"skill_bank_ids": [(6, 0, bank_ids)]})
    total = created + skipped
    _logger.info(
        "etp_assessment skill extract for prompt=%s: created=%s skipped=%s total=%s",
        prompt_record.id, created, skipped, total,
    )
    return {"created": created, "skipped": skipped, "total": total}


# Allowed A/B winners come from the resolved dimension set; the constant set is
# only the last-resort fallback when no dimensions resolve.


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
    # msq: a non-empty list (or a lone scalar) where every entry resolves.
    vals = ca if isinstance(ca, list) else ([ca] if ca is not None else [])
    return bool(vals) and all(one_ok(v) for v in vals)


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
    elif qtype == "subjective_rubric":
        r = it.get("rubric")
        if not isinstance(r, dict) or not all(
                k in r for k in ("checklist", "constraints", "pass_condition")):
            errs.append("subjective_rubric needs rubric "
                        "{checklist,constraints,pass_condition}")
    elif qtype == "subjective_justification":
        pass  # graded against the prompt; no rubric required
    elif qtype == "image_ab":
        specs = it.get("image_specs") or {}
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
    elif qtype == "image_text":
        specs = it.get("image_specs") or {}
        imgs = specs.get("images") or []
        if not isinstance(imgs, list) or not any(
                isinstance(i, dict) and i.get("prompt") for i in imgs):
            errs.append("image_text needs images[] with a prompt")
        key = specs.get("answer_key") or {}
        if not isinstance(key, dict) or not key.get("ideal_answer"):
            errs.append("image_text needs answer_key with ideal_answer")
    else:
        errs.append("unknown question_type %r" % qtype)
    return errs


def generate_questions(env, prompt_record, skill):
    system_prompt = _get_question_prompt(env)
    source_text = prompt_record._compiled_source_text()
    skill_artifacts = json.dumps({
        "name": skill.name,
        "description": skill.description or "",
        "tags": skill.tags or "",
        "question_type": skill.question_type,
        "question_count": skill.question_count,
        "difficulty": skill.difficulty,
    }, ensure_ascii=False)
    # Resolve A/B axes once and thread the SAME set through the prompt,
    # validation, and answer-key build so they can never disagree.
    ab_dims = _resolve_ab_dimensions(skill)
    if skill.question_type in _IMAGE_TYPES:
        directive = _image_question_directive(
            skill.question_type, skill.question_count, ab_dims=ab_dims)
    else:
        directive = (
            f"Generate exactly {skill.question_count} question(s) of type "
            f"'{skill.question_type}' for this skill as one JSON array."
        )
    user = (
        f"SOURCE MATERIAL:\n{source_text}\n\n"
        f"SKILL TO TEST:\n{skill_artifacts}\n\n"
        + directive
    )
    raw = _call_vertex(
        env, system_prompt, user, max_tokens=_GEN_MAX_OUTPUT_TOKENS,
        temperature=0.5,
        response_json=True,
        usage_ctx={"operation": "generate_questions",
                   "prompt_id": prompt_record.id, "skill_id": skill.id,
                   "note": skill.name},
    )
    items = _extract_json_array(raw)
    PromptQuestion = env["etp.assessment.pro.prompt.question"].sudo()
    draft_ids = []
    skipped_bad = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or it.get("title") or "").strip()
        prompt_text = (it.get("prompt") or "").strip()
        if not name and not prompt_text:
            continue
        qtype = it.get("question_type") if it.get("question_type") in _QUESTION_TYPES \
            else skill.question_type
        # Cohesion gate: skip (with a logged reason) any item missing the fields
        # its type requires, rather than writing a draft that later crashes scoring.
        violations = _validate_question_item(it, qtype, ab_dims=ab_dims)
        if violations:
            skipped_bad.append("%s: %s" % (
                (name or prompt_text[:40]), "; ".join(violations)))
            _logger.warning(
                "etp_assessment skipped malformed %s item for skill %s: %s",
                qtype, skill.name, "; ".join(violations))
            continue
        difficulty = it.get("difficulty") if it.get("difficulty") in _DIFFICULTIES \
            else skill.difficulty
        vals = {
            "prompt_id": prompt_record.id,
            "skill_id": skill.id,
            "name": (name or prompt_text[:60])[:200],
            "question_prompt": prompt_text or name,
            "question_type": qtype,
            "difficulty": difficulty,
        }
        if qtype in _IMAGE_TYPES:
            # Store the answer key + image briefs; rendering is decoupled to fix
            # the synchronous-render request crash.
            vals.update(_build_image_draft_fields(
                env, qtype, it, ab_dims=ab_dims,
                usage_ctx={"operation": "generate_image",
                           "prompt_id": prompt_record.id, "skill_id": skill.id,
                           "note": skill.name}))
            if vals.get("image_brief_json"):
                vals["image_state"] = "pending"
        else:
            options = it.get("options") or []
            vals.update({
                "options_json": json.dumps(options, ensure_ascii=False) if options else False,
                "correct_answer_json": json.dumps(
                    it.get("correct_answer"), ensure_ascii=False
                ) if it.get("correct_answer") is not None else False,
                "rubric_json": json.dumps(
                    it.get("rubric"), ensure_ascii=False
                ) if it.get("rubric") else False,
                "official_reasoning": it.get("official_reasoning") or False,
            })
        rec = PromptQuestion.create(vals)
        draft_ids.append(rec.id)
    _logger.info(
        "etp_assessment generated %s drafts for skill=%s on prompt=%s",
        len(draft_ids), skill.name, prompt_record.id,
    )
    return draft_ids
