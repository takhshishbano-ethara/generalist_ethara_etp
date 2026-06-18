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
        _param(env, "etp_assessment.vertex_project_id"),
        _param(env, "etp_assessment.vertex_location", "us-central1"),
        _param(env, "etp_assessment.vertex_model", "gemini-2.5-pro"),
        _param(env, "etp_assessment.vertex_api_key"),
    )


def _minted_bearer(env):
    ICP = env["ir.config_parameter"].sudo()
    sa_json = ICP.get_param(
        "etp_assessment.vertex_service_account_json", ""
    ) or ""
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
    ICP.set_param("etp_assessment.vertex_minted_token", token)
    ICP.set_param(
        "etp_assessment.vertex_minted_token_expires", str(now + expires_in)
    )
    if not ICP.get_param("etp_assessment.vertex_project_id") and sa.get("project_id"):
        ICP.set_param("etp_assessment.vertex_project_id", sa["project_id"])
    _logger.info(
        "etp_assessment minted Vertex bearer for %s (expires in %ss)",
        sa.get("client_email") or "?", expires_in,
    )
    return token


def _vertex_bearer(env):
    direct = _param(env, "etp_assessment.vertex_access_token")
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
        "(1) etp_assessment.vertex_api_key (AIza... or AQ...); "
        "(2) etp_assessment.vertex_access_token + vertex_project_id; "
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
        "etp_assessment.skill_gen_prompt", "") or "").strip()
    if p:
        return p
    bundled = _load_bundled_prompt("skill_gen.md")
    return bundled.strip() if bundled.strip() else INLINE_SKILL_GEN_PROMPT


def _get_question_prompt(env):
    p = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.question_prompt", "") or "").strip()
    if p:
        return p
    bundled = _load_bundled_prompt("question.md")
    return bundled.strip() if bundled.strip() else INLINE_QUESTION_PROMPT


def _call_vertex(env, system_prompt, user_text, max_tokens=4000, temperature=0.4):
    import httpx
    _project, _loc, model, _key = _vertex_creds(env)
    url, headers = _gemini_request(env, model, "generateContent")
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
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
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Vertex response missing text content: {str(data)[:300]}"
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


_QUESTION_TYPES = {"mcq", "msq", "subjective_justification", "subjective_rubric"}
_DIFFICULTIES = {"easy", "medium", "hard"}


def extract_skills(env, prompt_record):
    source_text = prompt_record._compiled_source_text()
    system_prompt = _get_skill_gen_prompt(env)
    raw = _call_vertex(
        env, system_prompt, source_text, max_tokens=3000, temperature=0.3
    )
    items = _extract_json_array(raw)
    Skill = env["etp.assessment.skill"].sudo()
    PromptSkill = env["etp.assessment.prompt.skill"].sudo()
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
    user = (
        f"SOURCE MATERIAL:\n{source_text}\n\n"
        f"SKILL TO TEST:\n{skill_artifacts}\n\n"
        f"Generate exactly {skill.question_count} question(s) of type "
        f"'{skill.question_type}' for this skill as one JSON array."
    )
    raw = _call_vertex(env, system_prompt, user, max_tokens=6000, temperature=0.5)
    items = _extract_json_array(raw)
    PromptQuestion = env["etp.assessment.prompt.question"].sudo()
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
        options = it.get("options") or []
        rec = PromptQuestion.create({
            "prompt_id": prompt_record.id,
            "skill_id": skill.id,
            "name": (name or prompt_text[:60])[:200],
            "question_prompt": prompt_text or name,
            "question_type": qtype,
            "difficulty": difficulty,
            "options_json": json.dumps(options, ensure_ascii=False) if options else False,
            "correct_answer_json": json.dumps(
                it.get("correct_answer"), ensure_ascii=False
            ) if it.get("correct_answer") is not None else False,
            "rubric_json": json.dumps(
                it.get("rubric"), ensure_ascii=False
            ) if it.get("rubric") else False,
        })
        draft_ids.append(rec.id)
    _logger.info(
        "etp_assessment generated %s drafts for skill=%s on prompt=%s",
        len(draft_ids), skill.name, prompt_record.id,
    )
    return draft_ids
