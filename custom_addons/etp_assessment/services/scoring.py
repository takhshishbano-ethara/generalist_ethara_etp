# -*- coding: utf-8 -*-
"""Subjective LLM scoring for etp_assessment (Vertex AI Gemini).

ONE Vertex call per candidate (batched): every needs_llm response is
bundled into a single prompt; the LLM returns a JSON array of
``{id, score, feedback}`` and we apply the configurable subjective pass
threshold (System Param ``etp_assessment.subjective_pass_threshold``) to
convert each 0..1 quality score to PASS/FAIL → full ``subjective_points``
or 0.

This module imports the low-level client from services/vertex.py and
extends it; vertex.py stays focused on the API transport.
"""
import json
import logging
import re

from . import vertex as vertex_svc

_logger = logging.getLogger(__name__)


DEFAULT_SCORING_PROMPT = (
    "You are an expert assessment grader for written-justification "
    "evaluation tasks. For each item in the candidate submission, decide "
    "how well the candidate_justification answers the question prompt, "
    "applying the rubric's pass_condition when present and the "
    "question prompt itself when no rubric is provided. "
    "Return ONLY a JSON array. Each element MUST be a JSON object with "
    'exactly these keys: {"id": <int>, "score": <float 0.0-1.0>, '
    '"feedback": "<2-3 sentence rationale>"}. No prose, no markdown.'
)


def _get_scoring_prompt(env):
    p = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.scoring_system_prompt", "") or "").strip()
    if p:
        return p
    return DEFAULT_SCORING_PROMPT


def _subjective_points(env):
    raw = env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.subjective_points", "10")
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        val = 10
    return val if val > 0 else 10


def _subjective_pass_threshold(env):
    raw = env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.subjective_pass_threshold", "0.7")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.7
    if val > 1.0:
        val = val / 100.0
    return val if 0.0 <= val <= 1.0 else 0.7


def _rubric_text(question):
    raw = (question.subjective_rubric_json or "").strip()
    if not raw or raw in ("[]", "{}"):
        return ""
    try:
        rubric = json.loads(raw)
    except Exception:
        return raw
    if isinstance(rubric, dict):
        rubric = [rubric]
    if not isinstance(rubric, list):
        return raw
    parts = []
    for f in rubric:
        if not isinstance(f, dict):
            continue
        label = f.get("label") or f.get("key") or "Field"
        block = [f"FIELD: {label}"]
        if f.get("checklist"):
            block.append("  Checklist:")
            block += [f"    - {c}" for c in f["checklist"]]
        if f.get("constraints"):
            block.append("  Constraints:")
            block += [f"    - {c}" for c in f["constraints"]]
        if f.get("pass_condition"):
            block.append(f"  Pass condition: {f['pass_condition']}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def _parse_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            raise ValueError(
                f"Could not parse JSON array from scoring response: {text[:200]}")
        parsed = json.loads(m.group(0))
    if isinstance(parsed, dict):
        if "results" in parsed and isinstance(parsed["results"], list):
            return parsed["results"]
        return [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Scoring response is not a JSON array: {text[:200]}")
    return parsed


def score_evaluator(env, evaluator):
    """Score one candidate's needs_llm responses in ONE Vertex call.

    Returns the count of responses scored. Per D2 the
    ``subjective_justification`` type is graded against an EMPTY rubric:
    the LLM judges purely from question prompt + candidate justification.
    """
    todo = evaluator.response_ids.filtered(
        lambda r: r.needs_llm and r.llm_state in (
            "not_needed", "pending", "queued", "failed"))
    if not todo:
        return 0

    items = []
    for resp in todo:
        q = resp.question_id
        # subjective_justification: pass empty rubric per D2.
        # subjective_rubric: include the question's rubric JSON.
        if q.question_type == "subjective_rubric":
            rubric = _rubric_text(q)
        else:
            rubric = ""
        items.append({
            "id": resp.id,
            "question_title": q.name or "",
            "question_type": q.question_type or "",
            "prompt": q.prompt or "",
            "description": q.description or "",
            "rubric": rubric,
            "candidate_justification": resp.justification or "",
        })

    system_prompt = _get_scoring_prompt(env)
    user_text = (
        "Score each candidate_justification on a 0.0 to 1.0 scale (1.0 = "
        "fully meets the bar, 0.0 = does not at all). Apply the rubric "
        "pass_condition where present; otherwise judge against the "
        "question prompt. Return ONLY a JSON array; each element MUST "
        'have keys {"id", "score", "feedback"}. The id MUST match the '
        "id from the input items below.\n\n"
        + json.dumps({"items": items}, ensure_ascii=False)
    )

    model_name = (env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.vertex_model", "gemini-2.5-flash-lite") or "gemini-2.5-flash-lite")
    _logger.info(
        "etp_assessment scoring: evaluator=%s items=%d model=%s",
        evaluator.id, len(items), model_name)

    raw = vertex_svc._call_vertex(
        env, system_prompt, user_text,
        max_tokens=600 + 400 * len(items),
        temperature=0.2,
    )
    parsed = _parse_array(raw)

    points = _subjective_points(env)
    threshold = _subjective_pass_threshold(env)
    by_id = {}
    for it in parsed:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("id") if it.get("id") is not None else it.get("item_id")
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        sc = it.get("score")
        try:
            score01 = float(sc) if sc is not None else 0.0
        except (TypeError, ValueError):
            score01 = 0.0
        if score01 > 1.0:
            score01 = score01 / 100.0 if score01 <= 100.0 else 1.0
        score01 = max(0.0, min(1.0, score01))
        by_id[rid] = {
            "score01": score01,
            "feedback": str(it.get("feedback") or it.get("reasoning") or ""),
        }

    scored = 0
    for resp in todo:
        r = by_id.get(resp.id)
        if not r:
            resp.write({
                "llm_state": "failed",
                "llm_attempts": (resp.llm_attempts or 0) + 1,
                "llm_feedback": "LLM did not return a score for this "
                                "response in the batched call.",
            })
            continue
        passed = r["score01"] >= threshold
        resp.write({
            "llm_state": "scored",
            "llm_raw_score": r["score01"],
            "llm_feedback": r["feedback"],
            "llm_score": points if passed else 0,
            "llm_max_score": points,
            "llm_passed": passed,
            "llm_attempts": (resp.llm_attempts or 0) + 1,
        })
        scored += 1
    return scored
