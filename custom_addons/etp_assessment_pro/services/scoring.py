# -*- coding: utf-8 -*-
"""Subjective LLM scoring for etp_assessment (Vertex AI Gemini).

ONE Vertex call per candidate (batched): every needs_llm response is
bundled into a single prompt; the LLM returns a JSON array of
``{id, score, feedback}`` and we apply the configurable subjective pass
threshold (System Param ``etp_assessment_pro.subjective_pass_threshold``) to
convert each 0..1 quality score to PASS/FAIL → full ``subjective_points``
or 0.

This module imports the low-level client from services/vertex.py and
extends it; vertex.py stays focused on the API transport.
"""
import json
import logging
import re

from . import vertex as vertex_svc
from . import consistency as consistency_svc

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
        "etp_assessment_pro.scoring_system_prompt", "") or "").strip()
    if p:
        return p
    return DEFAULT_SCORING_PROMPT


def _subjective_points(env):
    raw = env["ir.config_parameter"].sudo().get_param(
        "etp_assessment_pro.subjective_points", "10")
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        val = 10
    return val if val > 0 else 10


def _subjective_pass_threshold(env):
    raw = env["ir.config_parameter"].sudo().get_param(
        "etp_assessment_pro.subjective_pass_threshold", "0.7")
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
    """Score one candidate's needs_llm responses, grouped by question_type.

    Dispatches each group to its scorer (one Vertex call per group):
    subjective_* (justification/rubric), image_ab, image_text. Returns the
    total count of responses scored.
    """
    todo = evaluator.response_ids.filtered(
        lambda r: r.needs_llm and r.llm_state in (
            "not_needed", "pending", "queued", "failed"))
    if not todo:
        return 0
    subjective = todo.filtered(lambda r: r.question_type in (
        "subjective_justification", "subjective_rubric"))
    image_ab = todo.filtered(lambda r: r.question_type == "image_ab")
    image_text = todo.filtered(lambda r: r.question_type == "image_text")
    scored = 0
    if subjective:
        scored += _score_subjective_items(env, subjective)
    if image_ab:
        scored += _score_image_ab_items(env, image_ab)
    if image_text:
        scored += _score_image_text_items(env, image_text)
    return scored


def _score_subjective_items(env, todo):
    """Batched scoring for subjective_justification / subjective_rubric.

    Per D2 the ``subjective_justification`` type is graded against an EMPTY
    rubric: the LLM judges purely from question prompt + candidate
    justification.
    """
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
        "etp_assessment_pro.vertex_model", "gemini-3.1-pro-preview")
        or "gemini-3.1-pro-preview")
    _logger.info(
        "etp_assessment scoring: subjective items=%d model=%s",
        len(items), model_name)

    raw = vertex_svc._call_vertex(
        env, system_prompt, user_text,
        max_tokens=600 + 400 * len(items),
        temperature=0.2,
        usage_ctx={"operation": "score_subjective", "note": "subjective"},
    )
    parsed = _parse_array(raw)

    points = _subjective_points(env)
    threshold = _subjective_pass_threshold(env)
    by_id = {}
    for it in parsed:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("id") if it.get("id") is not None else it.get("item_id")
        if raw_id is None:
            continue
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


def _option_rating(name):
    n = (name or "").strip().lower()
    if n in ("response a", "a"):
        return "A"
    if n in ("response b", "b"):
        return "B"
    if "both good" in n:
        return "BG"
    if "both bad" in n:
        return "BB"
    if "tie" in n:
        return "Tie"
    return name or ""


def _dim_abbr(dimension_name):
    m = re.search(r"\(([A-Za-z]{1,4})\)", dimension_name or "")
    if m:
        return m.group(1).upper()
    return (dimension_name or "").strip().upper()


def _compose_ab_feedback(raw_it, precheck):
    parts = []
    fb = str(raw_it.get("feedback") or raw_it.get("reasoning_summary") or "")
    if fb:
        parts.append(fb)
    issues = raw_it.get("issues")
    if isinstance(issues, list) and issues:
        parts.append("Issues: " + "; ".join(str(i) for i in issues))
    flags = (precheck or {}).get("flags") or []
    if flags:
        parts.append("Consistency (%s): %s" % (
            (precheck or {}).get("severity", "none"),
            "; ".join(str(f.get("message", "")) for f in flags)))
    return "\n".join(parts)


def _score_image_ab_items(env, todo):
    points = _subjective_points(env)
    threshold = _subjective_pass_threshold(env)
    items = []
    flags_by_id = {}
    for resp in todo:
        q = resp.question_id
        official = {}
        candidate = {}
        tasker_ratings = {}
        for qd in q.question_dimension_ids:
            abbr = _dim_abbr(qd.dimension_id.name)
            correct = [
                _option_rating(ol.name)
                for ol in qd.option_line_ids.filtered("is_correct")]
            if correct:
                official[abbr] = correct[0] if len(correct) == 1 else correct
            chosen = [
                _option_rating(line.selected_option_id.name)
                for line in resp.line_ids
                if line.selected_option_id
                and line.dimension_id.id == qd.dimension_id.id]
            if chosen:
                candidate[abbr] = chosen[0] if len(chosen) == 1 else chosen
                tasker_ratings[abbr] = chosen[0]
        precheck = consistency_svc.consistency_checker(
            tasker_ratings, resp.justification or "")
        flags_by_id[resp.id] = precheck
        items.append({
            "id": resp.id,
            "question_title": q.name or "",
            "question_prompt": q.prompt or "",
            "official_ratings": official,
            "official_reasoning": q.official_reasoning or "",
            "candidate_ratings": candidate,
            "candidate_justification": resp.justification or "",
            "consistency_flags": precheck.get("flags", []),
            "consistency_severity": precheck.get("severity", "none"),
        })

    system_prompt = (
        "You are evaluating image A/B-comparison justifications. For each "
        "item, judge how well candidate_justification aligns with the "
        "official_ratings and official_reasoning, using consistency_flags as "
        "supporting signals (do not blindly punish substantively strong "
        "written reasoning). Return ONLY a JSON array; each element MUST have "
        'keys {"id": <int>, "score": <int 0-10>, "alignment": '
        '"low|medium|high", "strengths": [..], "issues": [..], '
        '"feedback": "<rationale>"}. No markdown, no prose.'
    )
    user_text = (
        "Score each candidate_justification 0-10 against the official answer "
        "key. The id MUST match the id from the input items below.\n\n"
        + json.dumps({"items": items}, ensure_ascii=False)
    )
    raw = vertex_svc._call_vertex(
        env, system_prompt, user_text,
        max_tokens=600 + 500 * len(items), temperature=0.2,
        usage_ctx={"operation": "score_subjective"})
    parsed = _parse_array(raw)

    by_id = {}
    for it in parsed:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("id") if it.get("id") is not None else it.get("item_id")
        if raw_id is None:
            continue
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        try:
            sc = float(it.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        score01 = max(0.0, min(1.0, sc / 10.0))
        by_id[rid] = {"score01": score01, "raw": it}

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
            "llm_feedback": _compose_ab_feedback(
                r["raw"], flags_by_id.get(resp.id)),
            "llm_score": int(round(r["score01"] * points)),
            "llm_max_score": points,
            "llm_passed": passed,
            "llm_attempts": (resp.llm_attempts or 0) + 1,
        })
        scored += 1
    return scored


def _score_image_text_items(env, todo):
    points = _subjective_points(env)
    threshold = _subjective_pass_threshold(env)
    items = []
    for resp in todo:
        q = resp.question_id
        key = {}
        raw_key = (q.subjective_rubric_json or "").strip()
        if raw_key and raw_key not in ("[]", "{}"):
            try:
                key = json.loads(raw_key)
            except Exception:
                key = {"scoring_guide": raw_key}
        if not isinstance(key, dict):
            key = {}
        items.append({
            "id": resp.id,
            "question_title": q.name or "",
            "question_prompt": q.prompt or "",
            "ideal_answer": key.get("ideal_answer", ""),
            "mandatory_elements": key.get("mandatory_elements", []),
            "penalty_rules": key.get("penalty_rules", []),
            "scoring_guide": key.get("scoring_guide", ""),
            "candidate_text": resp.justification or "",
        })

    system_prompt = (
        "You are grading image prompt-writing / description answers against a "
        "textual answer key. For each item, compare candidate_text to "
        "ideal_answer, require the mandatory_elements, and apply penalty_rules "
        "and scoring_guide. Return ONLY a JSON array; each element MUST have "
        'keys {"id": <int>, "score": <int 0-100>, "feedback": "<rationale>"}. '
        "No markdown, no prose."
    )
    user_text = (
        "Score each candidate_text 0-100 against its answer key. The id MUST "
        "match the id from the input items below.\n\n"
        + json.dumps({"items": items}, ensure_ascii=False)
    )
    raw = vertex_svc._call_vertex(
        env, system_prompt, user_text,
        max_tokens=600 + 500 * len(items), temperature=0.2,
        usage_ctx={"operation": "score_subjective"})
    parsed = _parse_array(raw)

    by_id = {}
    for it in parsed:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("id") if it.get("id") is not None else it.get("item_id")
        if raw_id is None:
            continue
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        try:
            sc = float(it.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        score01 = max(0.0, min(1.0, sc / 100.0))
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
            "llm_score": int(round(r["score01"] * points)),
            "llm_max_score": points,
            "llm_passed": passed,
            "llm_attempts": (resp.llm_attempts or 0) + 1,
        })
        scored += 1
    return scored
