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
# contract {question_id, score, max_score, feedback} is the stable part.
DEFAULT_SCORING_PROMPT = (
    "You are an expert assessment grader for AI-output evaluation tasks. "
    "You will receive a JSON payload with one candidate's submitted "
    "assessment plus, for image questions, the actual images referenced "
    "by index (e.g. [IMAGE 1] = Response A of question X). Question types: "
    "image_comparison (compare two AI-generated images against the "
    "instruction), text (written evaluation), coding (code review). "
    "For each question judge whether the candidate's dimension selections "
    "are correct given the evidence (LOOK at the images for image "
    "questions) and whether their justification shows real understanding. "
    "Return ONLY a JSON array, no prose. One item per question, exactly: "
    '{"question_id": <int, echo back unchanged>, "score": <int 0-10>, '
    '"max_score": 10, "feedback": "<2-3 sentence rationale>"}. '
    "Score every question in the payload."
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
    """Scoring rubric text for this question, or '' when none uploaded.

    Optional: rubrics are uploaded later via CSV on the assessment page.
    Looks up etp.assessment.rubric by (category + question_type), then
    category-only. Absence is fine — the grader falls back to the prompt.
    """
    if "etp.assessment.rubric" not in env:
        return ""
    Rubric = env["etp.assessment.rubric"].sudo()
    cat = question.category_id
    rec = Rubric.search([
        ("category_id", "=", cat.id),
        ("question_type", "=", question.question_type),
        ("active", "=", True),
    ], limit=1) or Rubric.search([
        ("category_id", "=", cat.id),
        ("question_type", "in", (False, "")),
        ("active", "=", True),
    ], limit=1)
    return rec.content if rec else ""


def _extract_json_object(text):
    """Pull the first JSON object out of an LLM response (tolerant)."""
    import re
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
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

    provider = (_param(env, "etp_assessment.llm_provider", "bedrock")
                or "bedrock").strip().lower()
    multimodal = provider == "vertex"

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
        "Grade ONLY the candidate_justification for this single question. "
        "If a rubric is present, apply it; otherwise judge against the "
        "question prompt and the candidate's dimension selections. "
        "Return ONLY a JSON object: "
        '{"score": <int 0-10>, "max_score": 10, "feedback": "<2-3 sentences>"}.'
        "\n\n" + json.dumps(item, ensure_ascii=False)
    )

    if multimodal:
        raw = _call_vertex(env, system_prompt, user_text,
                           max_tokens=1000, temperature=0.2,
                           image_parts=image_parts)
    else:
        raw = _call_bedrock(env, system_prompt, user_text,
                            max_tokens=1000, temperature=0.2)

    obj = _extract_json_object(raw)
    _logger.info(
        "etp_assessment subjective score: response=%s provider=%s images=%d",
        response.id, provider, len(image_parts))
    return {
        "score": int(obj.get("score") or 0),
        "max_score": int(obj.get("max_score") or 10),
        "feedback": str(obj.get("feedback") or ""),
    }


def build_scoring_payload(evaluator, with_images=True):
    """Serialize one candidate's submitted responses + collect image parts.

    Returns (payload_dict, image_parts). Images are indexed: the payload
    references "[IMAGE n]" so the model can connect parts to questions.
    """
    questions = []
    image_parts = []
    for resp in evaluator.response_ids.filtered(lambda r: r.state == "submitted"):
        q = resp.question_id
        dims = []
        for line in resp.line_ids:
            correct = q.question_dimension_ids.filtered(
                lambda qd: qd.dimension_id == line.dimension_id
            ).option_line_ids.filtered("is_correct")[:1]
            dims.append({
                "dimension": line.dimension_id.name or "",
                "selected_option": line.selected_option_id.name or "",
                "correct_option": correct.name if correct else None,
            })
        item = {
            "question_id": q.id,
            "title": q.name or "",
            "type": q.question_type or "text",
            "prompt": q.prompt or "",
            "description": q.description or "",
            "code_snippet": q.code_snippet or "",
            "dimensions": dims,
            "justification": resp.justification or "",
        }
        if q.question_type in ("image_comparison", "image_text") and with_images:
            refs = []
            for label, b64, mime in _question_images(q):
                if len(image_parts) >= MAX_IMAGES_PER_CALL:
                    refs.append(f"{label}: [omitted — image cap reached]")
                    continue
                image_parts.append({"mime_type": mime, "data": b64})
                refs.append(f"{label} = [IMAGE {len(image_parts)}]")
            item["images"] = refs
            # keep URLs as textual fallback context too
            item["image_a_url"] = q.image_a_url or ""
            item["image_b_url"] = q.image_b_url or ""
        questions.append(item)

    payload = {
        "assessment": evaluator.assessment_id.name or "",
        "candidate": evaluator.employee_id.name or "",
        "questions": questions,
    }
    return payload, image_parts


def score_evaluator(env, evaluator):
    """ONE LLM call: score every submitted response of one candidate.

    Vertex provider -> multimodal (model sees the images).
    Other providers -> text-only payload with image URLs as text.
    Returns {question_id: {score, max_score, feedback}}; raises on
    transport/parse errors (caller marks llm_state='failed', re-runnable).
    """
    provider = (_param(env, "etp_assessment.llm_provider", "bedrock")
                or "bedrock").strip().lower()
    multimodal = provider == "vertex"

    payload, image_parts = build_scoring_payload(
        evaluator, with_images=multimodal)
    if not payload["questions"]:
        return {}

    system_prompt = _get_scoring_prompt(env)
    user_text = json.dumps(payload, ensure_ascii=False)

    if multimodal:
        raw = _call_vertex(
            env, system_prompt, user_text,
            max_tokens=4000, temperature=0.2,
            image_parts=image_parts,
        )
    else:
        raw = _call_bedrock(
            env, system_prompt, user_text,
            max_tokens=4000, temperature=0.2,
        )

    results = _extract_json_array(raw)
    if not isinstance(results, list):
        raise ValueError(
            f"Scoring LLM did not return a JSON array: {str(raw)[:300]}")

    by_qid = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            qid = int(item.get("question_id"))
        except (TypeError, ValueError):
            continue
        by_qid[qid] = {
            "score": int(item.get("score") or 0),
            "max_score": int(item.get("max_score") or 10),
            "feedback": str(item.get("feedback") or ""),
        }

    _logger.info(
        "etp_assessment LLM scoring: evaluator=%s provider=%s multimodal=%s "
        "questions=%d images=%d results=%d",
        evaluator.id, provider, multimodal,
        len(payload["questions"]), len(image_parts), len(by_qid),
    )
    return by_qid
