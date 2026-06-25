import json
import logging
import os
import re
import time

_logger = logging.getLogger(__name__)

INLINE_SKILL_GEN_PROMPT = (
    "You are an expert assessment designer. Read the provided SOP / vendor / "
    "client documents and extract a JSON array of distinct skills a candidate "
    "must be tested on. Each item must be a JSON object with keys: "
    'name (string, short), description (string), tags (string, comma-separated), '
    'question_type (one of mcq/msq/subjective_justification/subjective_rubric), '
    'question_count (integer 3-10), time_minutes (integer 5-30), '
    'difficulty (easy/medium/hard). Return ONLY a JSON array, no markdown.'
)

INLINE_QUESTION_PROMPT = (
    "You are an expert assessment author. Generate questions for the given "
    "SKILL grounded in the supplied artifacts. Return ONLY a JSON array, no "
    "markdown. Each item: name (short title), prompt (full question text), "
    'question_type (mcq/msq/subjective_justification/subjective_rubric), '
    'options (list of strings for mcq/msq), correct_answer (string for mcq, '
    'list for msq, omit for subjective), rubric (object with checklist/'
    'constraints/pass_condition for subjective), difficulty (easy/medium/hard).'
)


def _param(env, key, default=""):
    val = env["ir.config_parameter"].sudo().get_param(key, default) or default
    if isinstance(val, str) and "PLACEHOLDER" in val:
        return default
    return val


def _vertex_creds(env):
    return (
        _param(env, "etp_assessment_pro.vertex_project_id"),
        # Gemini-3 series (text + image) is served on the ``global`` endpoint.
        _param(env, "etp_assessment_pro.vertex_location", "global"),
        _param(env, "etp_assessment_pro.vertex_model", "gemini-3.1-pro-preview"),
        _param(env, "etp_assessment_pro.vertex_api_key"),
    )


def _vertex_image_model(env):
    return _param(
        env, "etp_assessment_pro.vertex_image_model", "gemini-3-pro-image"
    )


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
            if location and location != "global"
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


# Best-effort USD rates per model: input/output per 1M tokens + per image.
# The logged token counts are exact; cost_usd is an estimate for budgeting and
# can be tuned here without touching the ledger data.
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
    """Write one row to the LLM usage ledger. Best-effort: a logging failure is
    swallowed so it never breaks a generation/scoring run."""
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
    except Exception:
        _logger.exception("etp_assessment: LLM usage log failed")


def _call_vertex(env, system_prompt, user_text, max_tokens=4000,
                 temperature=0.4, usage_ctx=None):
    import httpx
    _project, _loc, model, _key = _vertex_creds(env)
    url, headers = _gemini_request(env, model, "generateContent")
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
            # Gemini 2.5 "thinks" by default and those hidden thought tokens are
            # billed against maxOutputTokens — they can consume most of the
            # budget and TRUNCATE the JSON answer (unclosed array -> parse
            # error). Disable thinking so the full budget is available for the
            # structured output.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    _logger.info(
        "etp_assessment Vertex call: model=%s max_tokens=%d",
        model, max_tokens,
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
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Vertex response missing text content: {str(data)[:300]}"
        )


def generate_image(env, image_prompt, *, aspect_hint=None, usage_ctx=None):
    """Text->image via the Gemini image model (``:generateContent`` with
    responseModalities TEXT+IMAGE). Returns ``(b64_str, mime)`` ready for a
    Binary field / a ``data:`` URL. Raises RuntimeError when no image part
    comes back (e.g. a safety block) so the caller can keep the text draft and
    mark the image failed."""
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
            return inline["data"], mime
    raise RuntimeError(
        "Vertex image response had no image part (safety block / refusal?): "
        f"{str(data)[:300]}"
    )


def _extract_json_array(text):
    text = (text or "").strip()
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
    raise ValueError(
        "Could not parse JSON array from LLM response: %s" % text[:200]
    )


_QUESTION_TYPES = {
    "mcq", "msq", "subjective_justification", "subjective_rubric",
    "image_ab", "image_text",
}
_DIFFICULTIES = {"easy", "medium", "hard"}
_IMAGE_TYPES = {"image_ab", "image_text"}
# Full names for the A/B evaluation axes the generator emits as short codes.
_AB_DIM_NAMES = {
    "IF": "Instruction Following",
    "VQ": "Visual Quality",
    "LAI": "Less AI Generated",
    "OC": "Overall Choice",
}
_AB_CHOICES = ["Response A", "Response B", "Both Good", "Both Bad", "Tie"]


# A text->image prompt should be a DETAILED, self-contained "brief": every
# visually deciding detail spelled out, with any on-image text quoted exactly so
# the model renders it legibly. This (placeholder-authoritative briefs) is what
# drives high-quality, evidence-grade images in the reference pipeline.
_IMG_PROMPT_RULE = (
    "Each image prompt MUST be a DETAILED, self-contained scene brief that "
    "states EVERY visually deciding detail (subjects, layout/composition, "
    "colours, materials, lighting) and QUOTES verbatim any text/labels/numbers "
    "that must appear in the image so it renders legibly. Default to a "
    "photorealistic style unless the scenario requires otherwise. Write it as a "
    "single source of truth, not a vague caption."
)


def _image_question_directive(qtype, count):
    """Directive appended to the question prompt so the LLM returns rich
    text->image briefs + the answer key in a shape we can render images from."""
    base = (
        f"Generate exactly {count} question(s) of type '{qtype}' as one JSON "
        "array. Do NOT emit mcq-style \"options\"/\"correct_answer\". Every "
        "item MUST contain a non-empty \"image_specs\" object. " + _IMG_PROMPT_RULE + " "
    )
    if qtype == "image_ab":
        return base + (
            'Shape: {"name": "...", "question_type": "image_ab", "prompt": '
            '"...", "difficulty": "medium", "image_specs": {"image_a_prompt": '
            '"detailed self-contained brief for Response A", "image_b_prompt": '
            '"detailed self-contained brief for Response B (must DIFFER from A '
            'in the deciding detail)", "dimensions": {"IF": "Response A", "VQ": '
            '"Response B", "LAI": "Tie", "OC": "Response A"}, '
            '"official_reasoning": "why those ratings are correct"}}. Each '
            "dimension value MUST be one of: Response A, Response B, Both Good, "
            "Both Bad, Tie. image_a_prompt and image_b_prompt are REQUIRED and "
            "must differ."
        )
    return base + (
        'Shape: {"name": "...", "question_type": "image_text", "prompt": '
        '"...", "difficulty": "medium", "image_specs": {"images": [{"slot": '
        '"single", "label": "Image", "prompt": "detailed self-contained brief '
        'for the stimulus"}], "answer_key": {"ideal_answer": "...", '
        '"mandatory_elements": ["..."], "penalty_rules": ["..."], '
        '"scoring_guide": "..."}}}. The image "prompt" is REQUIRED.'
    )


def _image_brief(scene):
    """Wrap a scene description in the reference pipeline's strict render brief
    so the image model treats it as the single source of truth and renders any
    quoted text legibly."""
    return (
        "Generate exactly one image and no other text: a clean, photorealistic "
        "image. It must show every detail this brief states; the brief is the "
        "single source of truth, follow it alone and treat it as the only "
        "attempt at the scene. Render any quoted text, labels, and numbers "
        "exactly and legibly:\n" + (scene or "")
    )


def _build_image_draft_fields(env, qtype, item, usage_ctx=None):
    """Generate the image(s) for one image draft and return a dict of stage's
    EXISTING draft fields (images_json / dimensions_json / official_reasoning /
    rubric_json). Image generation failures are logged and skipped so the text
    draft still survives (the reviewer can re-generate or upload)."""
    specs = item.get("image_specs") or {}
    vals = {}
    images = []

    def _gen(prompt_text, slot, label):
        if not prompt_text:
            return
        try:
            b64, mime = generate_image(
                env, _image_brief(prompt_text), usage_ctx=usage_ctx)
            images.append({
                "slot": slot, "label": label,
                "data": "data:%s;base64,%s" % (mime, b64),
            })
        except Exception as exc:
            _logger.warning(
                "etp_assessment image gen failed (%s/%s): %s",
                qtype, slot, repr(exc)[:160])

    if qtype == "image_ab":
        _gen(specs.get("image_a_prompt"), "a", "Response A")
        _gen(specs.get("image_b_prompt"), "b", "Response B")
        dim_specs = [
            {"label": _AB_DIM_NAMES.get(code, code),
             "options": _AB_CHOICES,
             "correct": [val] if val else []}
            for code, val in (specs.get("dimensions") or {}).items()
        ]
        if dim_specs:
            vals["dimensions_json"] = json.dumps(dim_specs, ensure_ascii=False)
        if specs.get("official_reasoning"):
            vals["official_reasoning"] = specs["official_reasoning"]
    else:  # image_text
        for img in (specs.get("images") or []):
            _gen(img.get("prompt"), img.get("slot") or "single",
                 img.get("label") or "Image")
        answer_key = specs.get("answer_key") or {}
        if answer_key:
            vals["rubric_json"] = json.dumps(answer_key, ensure_ascii=False)

    if images:
        vals["images_json"] = json.dumps(images, ensure_ascii=False)
    return vals


def extract_skills(env, prompt_record):
    source_text = prompt_record._compiled_source_text()
    system_prompt = _get_skill_gen_prompt(env)
    raw = _call_vertex(
        env, system_prompt, source_text, max_tokens=3000, temperature=0.3,
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
    if skill.question_type in _IMAGE_TYPES:
        directive = _image_question_directive(
            skill.question_type, skill.question_count)
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
        env, system_prompt, user, max_tokens=6000, temperature=0.5,
        usage_ctx={"operation": "generate_questions",
                   "prompt_id": prompt_record.id, "skill_id": skill.id,
                   "note": skill.name},
    )
    items = _extract_json_array(raw)
    PromptQuestion = env["etp.assessment.pro.prompt.question"].sudo()
    draft_ids = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or it.get("title") or "").strip()
        prompt_text = (it.get("prompt") or "").strip()
        if not name and not prompt_text:
            continue
        qtype = it.get("question_type") if it.get("question_type") in _QUESTION_TYPES \
            else skill.question_type
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
            # Image types: generate the picture(s) + answer key into stage's
            # existing draft fields (images_json/dimensions_json/...).
            vals.update(_build_image_draft_fields(
                env, qtype, it,
                usage_ctx={"operation": "generate_image",
                           "prompt_id": prompt_record.id, "skill_id": skill.id,
                           "note": skill.name}))
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
            })
        rec = PromptQuestion.create(vals)
        draft_ids.append(rec.id)
    _logger.info(
        "etp_assessment generated %s drafts for skill=%s on prompt=%s",
        len(draft_ids), skill.name, prompt_record.id,
    )
    return draft_ids
