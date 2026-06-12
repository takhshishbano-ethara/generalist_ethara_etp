# -*- coding: utf-8 -*-
"""Multimodal LLM scoring for etp_assessment.

ONE scoring system prompt covering the question types in use
(image_comparison / text / coding). For image questions the model SEES the
actual A/B images: binary images are inlined; URL-only images are fetched
and inlined (Vertex provider). Bedrock/OpenRouter providers fall back to
text-only payloads (URLs included as text).

Batching: ONE LLM call per candidate (all questions in one payload).
Images are attached per-call; if a candidate has many image questions the
call simply carries more parts.

System prompt slot (research team pastes the final wording here):
  etp_assessment.scoring_system_prompt
"""
import base64
import json
import logging

from .bedrock_questions import (
    _call_bedrock,
    _call_vertex,
    _extract_json_array,
    _param,
)

_logger = logging.getLogger(__name__)

# Placeholder until the research team delivers the real prompt. The output
# contract {score, feedback} is the stable part: a 0..1 quality score. WE
# convert it to PASS/FAIL against the configurable subjective threshold.
DEFAULT_SCORING_PROMPT = (
    "You are an expert assessment grader for AI-output evaluation tasks. "
    "You will receive a JSON payload describing ONE candidate answer plus, "
    "for image questions, the actual images referenced by index (e.g. "
    "[IMAGE 1] = Response A). Question types: image_comparison (compare two "
    "AI-generated images against the instruction), text (written evaluation), "
    "coding (code review). Score how well the candidate's justification meets "
    "the rubric's pass_condition (or, with no rubric, shows correct reasoning "
    "aligned with the answer key and the dimension selections — LOOK at the "
    "images for image questions) on a 0.0 to 1.0 scale. "
    "Return ONLY a JSON object, no prose, exactly: "
    '{"score": <float 0.0-1.0>, "feedback": "<2-3 sentence rationale>"}.'
)

MAX_IMAGES_PER_CALL = 16  # safety cap on inline parts per scoring call
IMAGE_FETCH_TIMEOUT = 20


def _get_scoring_prompt(env):
    return _param(env, "etp_assessment.scoring_system_prompt") \
        or DEFAULT_SCORING_PROMPT


def _guess_mime(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:3] == b"GIF":
        return "image/gif"
    return "image/png"


def _fetch_image_b64(url):
    """Download an image URL -> (b64, mime). Returns (None, None) on failure."""
    import httpx
    try:
        with httpx.Client(
            timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200 or not resp.content:
            return None, None
        raw = resp.content
        return base64.b64encode(raw).decode(), _guess_mime(raw)
    except Exception:
        _logger.warning("Scoring: could not fetch image %s", url[:120])
        return None, None


def _question_images(question):
    """Yield (label, b64, mime) for a question's images (binary or URL).

    Bank questions (etp.assessment.question) are URL-only; prompt drafts
    carry binary. Field access is defensive so both models work.
    """
    out = []
    for bin_field, url_field, label in (
        ("image_a", "image_a_url", "Response A"),
        ("image_b", "image_b_url", "Response B"),
    ):
        binary = question[bin_field] if bin_field in question._fields else False
        if binary:
            raw = base64.b64decode(binary)
            out.append((label, base64.b64encode(raw).decode(), _guess_mime(raw)))
            continue
        url = question[url_field] if url_field in question._fields else False
        if url:
            b64, mime = _fetch_image_b64(url)
            if b64:
                out.append((label, b64, mime))
    return out


def _rubric_for(env, question):
    """Scoring rubric text for this question, or '' when none.

    Reads the question's imported/generated subjective rubric
    (subjective_rubric_json: [{key,label,checklist,constraints,
    pass_condition}]) and renders it as grading guidance for the LLM.
    Absence is fine — the grader falls back to the prompt.
    """
    raw = getattr(question, "subjective_rubric_json", "") or ""
    raw = raw.strip()
    if not raw or raw in ("[]", "{}"):
        return ""
    try:
        rubric = json.loads(raw)
    except Exception:
        return raw  # malformed -> hand the text through verbatim
    if not isinstance(rubric, list):
        return raw
    parts = []
    for f in rubric:
        if not isinstance(f, dict):
            continue
        label = f.get("label") or f.get("key") or "Field"
        block = [f"FIELD: {label}"]
        if f.get("checklist"):
            block.append("  Checklist (candidate should cover):")
            block += [f"    - {c}" for c in f["checklist"]]
        if f.get("constraints"):
            block.append("  Constraints:")
            block += [f"    - {c}" for c in f["constraints"]]
        if f.get("pass_condition"):
            block.append(f"  Pass condition: {f['pass_condition']}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def _extract_json_object(text):
    """Pull the first JSON object out of an LLM response (tolerant).

    Tolerates the model returning a 1-element array (the seeded scoring
    prompt is array-shaped) — unwraps the first object.
    """
    import re
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    def _unwrap(val):
        if isinstance(val, list):
            for it in val:
                if isinstance(it, dict):
                    return it
            raise ValueError("scoring array had no object")
        return val

    try:
        return _unwrap(json.loads(text))
    except ValueError:
        raise
    except Exception:
        pass
    # try object first, then array
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return _unwrap(json.loads(m.group(0)))
        except Exception:
            pass
    raise ValueError(
        "Could not parse JSON object from scoring response: %s" % text[:200])


def score_one_response(env, response):
    """Score ONE response's subjective justification. Multimodal on Vertex.

    The per-question RabbitMQ consumer / cron drainer entry point.
    Returns {score, max_score, feedback}; raises on transport/parse error.
    """
    q = response.question_id
    dims = []
    for line in response.line_ids:
        correct = q.question_dimension_ids.filtered(
            lambda qd: qd.dimension_id == line.dimension_id
        ).option_line_ids.filtered("is_correct")[:1]
        dims.append({
            "dimension": line.dimension_id.name or "",
            "selected_option": line.selected_option_id.name or "",
            "correct_option": correct.name if correct else None,
        })

    # Single provider: Vertex AI Gemini (multimodal — the model sees images).
    multimodal = True

    item = {
        "question_id": q.id,
        "title": q.name or "",
        "type": q.question_type or "text",
        "prompt": q.prompt or "",
        "description": q.description or "",
        "code_snippet": (q.code_snippet or "") if "code_snippet" in q._fields else "",
        "dimensions": dims,
        "candidate_justification": response.justification or "",
        "rubric": _rubric_for(env, q),
    }
    image_parts = []
    if multimodal and q.question_type in ("image_comparison", "image_text"):
        refs = []
        for label, b64, mime in _question_images(q):
            image_parts.append({"mime_type": mime, "data": b64})
            refs.append(f"{label} = [IMAGE {len(image_parts)}]")
        if refs:
            item["images"] = refs

    system_prompt = _get_scoring_prompt(env)
    user_text = (
        "Score the candidate_justification for this single question on a "
        "0.0 to 1.0 scale (1.0 = fully meets the bar, 0.0 = does not at all). "
        "Apply the rubric if present (its pass_condition is authoritative); "
        "otherwise judge against the question prompt and the candidate's "
        "dimension selections. "
        "Return ONLY a JSON object: "
        '{"score": <float 0.0-1.0>, "feedback": "<2-3 sentence rationale>"}.'
        "\n\n" + json.dumps(item, ensure_ascii=False)
    )

    raw = _call_vertex(env, system_prompt, user_text,
                       max_tokens=1000, temperature=0.2,
                       image_parts=image_parts)

    obj = _extract_json_object(raw)
    # The LLM returns a 0..1 quality score; WE decide pass/fail against the
    # configurable subjective threshold. Tolerate legacy reply shapes:
    #   - {"score": 0..1}              -> use directly
    #   - {"score": int, "max_score"}  -> normalize to 0..1
    #   - {"passed": bool}             -> 1.0 / 0.0
    if "score" in obj and obj.get("score") is not None:
        sc = float(obj.get("score") or 0)
        mx = float(obj.get("max_score") or 0)
        score01 = (sc / mx) if mx else sc
    elif "passed" in obj:
        score01 = 1.0 if bool(obj.get("passed")) else 0.0
    else:
        score01 = 0.0
    score01 = max(0.0, min(1.0, score01))
    _logger.info(
        "etp_assessment subjective score: response=%s score=%.2f images=%d",
        response.id, score01, len(image_parts))
    return {
        "score01": score01,
        "feedback": str(obj.get("feedback") or ""),
    }
