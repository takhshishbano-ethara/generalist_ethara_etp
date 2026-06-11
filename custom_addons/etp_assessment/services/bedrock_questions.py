# -*- coding: utf-8 -*-
"""Bedrock question-generation service for etp_assessment.

Reuses the bearer-token + inference-ARN Converse pattern proven in leviathan.
Reads credentials from System Parameters:
  etp_assessment.bedrock_inference_arn
  etp_assessment.bedrock_region
  etp_assessment.bedrock_bearer_token
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


def _bedrock_creds(env):
    return (
        _param(env, "etp_assessment.bedrock_inference_arn"),
        _param(env, "etp_assessment.bedrock_region", "us-east-1"),
        _param(env, "etp_assessment.bedrock_bearer_token"),
    )


def _call_bedrock(env, system_prompt, user_text, max_tokens=4000, temperature=0.4):
    """Provider-dispatching LLM call (name kept for backward compatibility).

    etp_assessment.llm_provider = "bedrock" (default) | "openrouter"
    """
    provider = (_param(env, "etp_assessment.llm_provider", "bedrock")
                or "bedrock").strip().lower()
    if provider == "openrouter":
        return _call_openrouter(env, system_prompt, user_text, max_tokens, temperature)
    return _call_bedrock_converse(env, system_prompt, user_text, max_tokens, temperature)


def _call_openrouter(env, system_prompt, user_text, max_tokens=4000, temperature=0.4):
    """OpenRouter chat-completions call (OpenAI-compatible schema).

    System Parameters:
      etp_assessment.openrouter_api_key  - sk-or-... key
      etp_assessment.openrouter_model    - e.g. "google/gemini-3-pro" or
                                           "anthropic/claude-sonnet-4.5"
    """
    import httpx

    api_key = _param(env, "etp_assessment.openrouter_api_key")
    model = _param(env, "etp_assessment.openrouter_model")
    if not api_key or not model:
        raise ValueError(
            "OpenRouter not configured. Set etp_assessment.openrouter_api_key "
            "and etp_assessment.openrouter_model in Settings > Technical > "
            "System Parameters (or switch etp_assessment.llm_provider back "
            "to 'bedrock')."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://www.ethara.com",
        "X-Title": "ETP Assessment",
    }

    _logger.info("etp_assessment OpenRouter call: model=%s max_tokens=%d", model, max_tokens)
    with httpx.Client(timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30)) as client:
        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter error [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"OpenRouter response missing content: {str(data)[:300]}"
        )


def _call_bedrock_converse(env, system_prompt, user_text, max_tokens=4000, temperature=0.4):
    import httpx

    arn, region, token = _bedrock_creds(env)
    if not arn or not token:
        raise ValueError(
            "Bedrock not configured. Set etp_assessment.bedrock_inference_arn and "
            "etp_assessment.bedrock_bearer_token in Settings > Technical > System Parameters."
        )

    model_id = urllib.parse.quote(arn, safe="")
    endpoint = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
    payload = {
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    _logger.info("etp_assessment Bedrock call: region=%s max_tokens=%d", region, max_tokens)
    with httpx.Client(timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30)) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Bedrock error [{resp.status_code}]: {resp.text[:400]}")
    data = resp.json()
    return data["output"]["message"]["content"][0]["text"]


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
    raw = _call_bedrock(env, sp, source_text, max_tokens=2000, temperature=0.3)
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
    raw = _call_bedrock(env, sp, user, max_tokens=max_tokens, temperature=0.5)
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
    """Generate questions for ALL skills in ONE Bedrock call (minimize calls).

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
    raw = _call_bedrock(env, sp, user, max_tokens=max_tokens, temperature=0.5)
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
            # bedrock_images.py turns them into actual images on demand.
            "image_prompt_a": str(it.get("image_prompt_a", ""))[:1024],
            "image_prompt_b": str(it.get("image_prompt_b", ""))[:1024],
        })
    return out
