# -*- coding: utf-8 -*-
"""Multimodal LLM scoring for etp_assessment (Vertex AI Gemini).

ONE scoring system prompt covering the question types in use
(image_comparison / text / coding). For image questions the model SEES the
actual A/B images: binary images are inlined; URL-only images are fetched
and inlined. Single provider — Vertex AI Gemini multimodal.

Batching: ONE LLM call per candidate (all questions in one payload).
Images are attached per-call; if a candidate has many image questions the
call simply carries more parts.

System prompt slot (research team pastes the final wording here):
  etp_assessment.scoring_system_prompt
"""
import base64
import json
import logging

from .vertex_questions import (
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
    """Return the active scoring prompt, in precedence order:
    1. System Parameter `etp_assessment.scoring_system_prompt` (live, no-deploy)
    2. The bundled `prompts/scoring_master.md` (research-team default)
    3. The terse inline DEFAULT_SCORING_PROMPT (last-ditch fallback)
    """
    p = _param(env, "etp_assessment.scoring_system_prompt", "")
    if p and p.strip():
        return p
    from .vertex_questions import _load_bundled_prompt
    bundled = _load_bundled_prompt("scoring_master.md")
    if bundled and bundled.strip():
        return bundled
    return DEFAULT_SCORING_PROMPT


def _guess_mime(raw):
    if not raw:
        return None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:3] == b"GIF":
        return "image/gif"
    if len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
        b"hevm", b"hevs", b"mif1", b"msf1",
    ):
        return "image/heic"
    return None


def _fetch_image_b64(url):
    """Download an image URL -> (b64, mime). Returns (None, None) on failure.

    Drops the response when Content-Type isn't an image OR the bytes don't
    carry a recognized image magic. Without this, hosts that serve HTML for
    pseudo-image URLs (Google Images search result pages, blog articles,
    expired CDN tokens with HTML error pages) would be base64-encoded and
    forwarded to Gemini, which then returns 400 "Provided image is not
    valid." — costing a Vertex call per bad URL.
    """
    import httpx
    try:
        with httpx.Client(
            timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200 or not resp.content:
            return None, None
        ctype = (resp.headers.get("content-type") or "").lower()
        ctype = ctype.split(";", 1)[0].strip()
        if ctype and not ctype.startswith("image/"):
            _logger.warning(
                "Scoring: %s returned Content-Type=%s (not an image); "
                "skipping multimodal attachment",
                url[:120], ctype,
            )
            return None, None
        raw = resp.content
        mime = _guess_mime(raw)
        if not mime:
            _logger.warning(
                "Scoring: %s bytes don't carry a recognized image magic "
                "(%s); skipping multimodal attachment",
                url[:120], raw[:8].hex() if raw else "empty",
            )
            return None, None
        return base64.b64encode(raw).decode(), mime
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
            mime = _guess_mime(raw)
            if not mime:
                _logger.warning(
                    "Scoring: question %s %s binary doesn't carry a "
                    "recognized image magic (%s); skipping attachment",
                    question.id, label,
                    raw[:8].hex() if raw else "empty")
                continue
            out.append((label, base64.b64encode(raw).decode(), mime))
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


def score_evaluator(env, evaluator):
    """Batched: score ALL needs_llm responses for one evaluator in ONE call.

    Returns dict {response_id: {"score01": float, "feedback": str}}.

    Why this exists: per-response scoring (score_one_response) pays the
    system-prompt + auth + connection overhead on every question. For a
    candidate with 20 questions that's 20 calls instead of 1. This batched
    path bundles them into one Vertex call (the LLM returns a JSON array
    of {id, score, feedback}), which is 5-20x cheaper and faster.

    Image attachments are accumulated across all questions and capped at
    MAX_IMAGES_PER_CALL (Gemini's safe inline limit). Overflow responses
    are scored text-only and a warning is logged with the affected IDs.

    Raises on transport/parse error so the caller can fall back to the
    per-response path for that evaluator (preserving the recovery contract
    callers already rely on).
    """
    todo = evaluator.response_ids.filtered(
        lambda r: r.needs_llm and r.llm_state in (
            "not_needed", "pending", "queued", "failed"))
    if not todo:
        return {}

    items = []
    image_parts = []
    text_only_due_to_image_cap = []
    for resp in todo:
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
            "id": resp.id,
            "question_title": q.name or "",
            "question_type": q.question_type or "text",
            "prompt": q.prompt or "",
            "description": q.description or "",
            "code_snippet": (q.code_snippet or "")
                if "code_snippet" in q._fields else "",
            "dimensions": dims,
            "candidate_justification": resp.justification or "",
            "rubric": _rubric_for(env, q),
        }
        if q.question_type in ("image_comparison", "image_text"):
            refs = []
            for label, b64, mime in _question_images(q):
                if len(image_parts) >= MAX_IMAGES_PER_CALL:
                    text_only_due_to_image_cap.append(resp.id)
                    break
                image_parts.append({"mime_type": mime, "data": b64})
                refs.append(f"{label} = [IMAGE {len(image_parts)}]")
            if refs:
                item["images"] = refs
        items.append(item)

    if text_only_due_to_image_cap:
        _logger.warning(
            "Batched score evaluator=%s hit MAX_IMAGES_PER_CALL=%d; "
            "responses scored text-only: %s",
            evaluator.id, MAX_IMAGES_PER_CALL, text_only_due_to_image_cap)

    system_prompt = _get_scoring_prompt(env)

    # Build the assessment-prompt-master envelope: ASSESSMENT bank +
    # IDS + REFERENCES + <submission>. Each item carries its own grading
    # block (checklist/constraints/pass_condition) extracted from the
    # question's subjective_rubric_json, so the LLM applies the per-field
    # rubric the seed system produced upstream.
    bank_items = []
    for resp in todo:
        q = resp.question_id
        grading_block = {"justification": {
            "type": "subjective", "checklist": [],
            "constraints": [], "pass_condition": "",
        }}
        try:
            rubric = json.loads(q.subjective_rubric_json or "[]")
            if isinstance(rubric, list) and rubric and isinstance(rubric[0], dict):
                f = rubric[0]
                grading_block["justification"] = {
                    "type": "subjective",
                    "checklist": f.get("checklist") or [],
                    "constraints": f.get("constraints") or [],
                    "pass_condition": f.get("pass_condition") or "",
                }
        except Exception:
            pass
        media = []
        if q.question_type in ("image_comparison", "image_text"):
            if "image_a_url" in q._fields and q.image_a_url:
                media.append({"label": "Response A", "type": "image",
                              "placeholder": "", "url": q.image_a_url})
            if "image_b_url" in q._fields and q.image_b_url:
                media.append({"label": "Response B", "type": "image",
                              "placeholder": "", "url": q.image_b_url})
        bank_items.append({
            "id": str(resp.id),
            "project": q.category_id.name or "",
            "inputs": {
                "prompt": q.prompt or "",
                "instruction": q.description or "",
                "media": media,
            },
            "fields": [{"key": "justification", "label": "Justification",
                        "type": "free_text", "options": []}],
            "grading": grading_block,
            "meta": {},
        })
    submission_payload = {
        str(r.id): {"justification": r.justification or ""} for r in todo
    }
    ids_block = {
        "worker_id": str(evaluator.employee_id.id) if evaluator.employee_id else None,
        "attempt_id": str(evaluator.id),
        "grading_block_hash": None,
    }
    user_text = (
        f"ASSESSMENT:\n"
        f"{json.dumps({'question_bank': bank_items}, ensure_ascii=False)}\n\n"
        f"IDS: {json.dumps(ids_block)}\n\n"
        f"REFERENCES: {{}}\n\n"
        f"<submission>\n"
        f"{json.dumps(submission_payload, ensure_ascii=False)}\n"
        f"</submission>"
    )

    raw = _call_vertex(
        env, system_prompt, user_text,
        max_tokens=600 + 400 * len(todo),
        temperature=0.2,
        image_parts=image_parts,
    )

    # Parse the results[] envelope (new) OR a bare array (legacy) OR a
    # single object (very old). Tolerant of all three so swapping the
    # scoring prompt back and forth doesn't break the pipeline.
    import re as _re
    cleaned = (raw or "").strip()
    cleaned = _re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = _re.sub(r"```$", "", cleaned).strip()
    parsed_obj = None
    try:
        parsed_obj = json.loads(cleaned)
    except Exception:
        parsed_obj = None

    submission_flags = []
    if isinstance(parsed_obj, dict) and "results" in parsed_obj:
        results = parsed_obj.get("results") or []
        submission_flags = parsed_obj.get("submission_flags") or []
    elif isinstance(parsed_obj, list):
        results = parsed_obj
    elif isinstance(parsed_obj, dict):
        results = [parsed_obj]
    else:
        try:
            arr = _extract_json_array(raw)
            results = arr if isinstance(arr, list) else []
        except Exception:
            results = []

    by_id = {}
    for it in results:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("item_id") if "item_id" in it else it.get("id")
        try:
            rid = int(raw_id) if raw_id is not None else 0
        except (TypeError, ValueError):
            rid = 0
        if not rid:
            continue
        gate = str(it.get("gate") or "none")
        # Gated answers are 0.00 by contract — never trust an LLM-returned
        # score on a gate (the prompt also writes 0 but be defensive).
        if gate != "none":
            score01 = 0.0
        elif "score" in it and it.get("score") is not None:
            sc = float(it.get("score") or 0)
            mx = float(it.get("max_score") or 0)
            score01 = (sc / mx) if mx else sc
        elif "passed" in it:
            score01 = 1.0 if bool(it.get("passed")) else 0.0
        else:
            score01 = 0.0
        score01 = max(0.0, min(1.0, score01))
        reasoning = it.get("reasoning") or it.get("feedback") or ""
        flags = it.get("flags") or []
        if not isinstance(flags, list):
            flags = []
        by_id[rid] = {
            "score01": score01,
            "feedback": str(reasoning),
            "gate": gate,
            "verdict_consistency": str(
                it.get("verdict_consistency") or "not_applicable"),
            "flags": ",".join(str(f) for f in flags),
        }

    for resp in todo:
        if resp.id not in by_id:
            by_id[resp.id] = {
                "score01": 0.0,
                "feedback": "LLM did not return a score for this response "
                            "in the batched call.",
                "gate": "none",
                "verdict_consistency": "not_applicable",
                "flags": "",
            }

    if submission_flags:
        _logger.info(
            "etp_assessment evaluator=%s submission_flags=%s",
            evaluator.id, submission_flags)
    _logger.info(
        "etp_assessment batched score: evaluator=%s responses=%d images=%d "
        "parsed=%d",
        evaluator.id, len(todo), len(image_parts), len(by_id))
    return by_id
