# -*- coding: utf-8 -*-
"""Bedrock LLM scoring service for etp_assessment.

Scores a candidate's submitted responses (selections + justifications) in ONE
Bedrock Converse call per candidate (batch — never per-response loops).

Reuses the same credential System Parameters as question generation:
  etp_assessment.bedrock_inference_arn
  etp_assessment.bedrock_region
  etp_assessment.bedrock_bearer_token

Scoring system prompt slot (paste the research team's prompt here, no deploy):
  etp_assessment.scoring_system_prompt
"""
import json
import logging

from .bedrock_questions import _call_bedrock, _extract_json_array

_logger = logging.getLogger(__name__)

# Placeholder until the research team delivers the real prompt.
# The contract (input shape + required output JSON) is the stable part —
# wording can change freely via the System Parameter.
DEFAULT_SCORING_PROMPT = (
    "You are an expert assessment grader. You will receive a JSON payload "
    "describing one candidate's submitted assessment: for each question you "
    "get the question prompt, its type, the rating dimensions with the option "
    "the candidate selected and the correct option (when defined), and the "
    "candidate's written justification. Grade the QUALITY of the candidate's "
    "reasoning and correctness. Return ONLY a JSON array, no prose. One item "
    "per question, exactly: "
    '{"question_id": <int, echo back unchanged>, '
    '"score": <int 0-10>, '
    '"max_score": 10, '
    '"feedback": "<2-3 sentence rationale for the score>"}. '
    "Score every question in the payload — do not skip any."
)


def _get_scoring_prompt(env):
    return env["ir.config_parameter"].sudo().get_param(
        "etp_assessment.scoring_system_prompt", ""
    ) or DEFAULT_SCORING_PROMPT


def build_scoring_payload(evaluator):
    """Serialize one candidate's submitted responses for the LLM.

    Auto-submitted placeholder responses (violation / time expiry) are
    included with their marker justification so the model can see gaps,
    but callers may filter them out via the assessment flag later.
    """
    questions = []
    for resp in evaluator.response_ids.filtered(lambda r: r.state == "submitted"):
        q = resp.question_id
        dims = []
        for line in resp.line_ids:
            # correct option for this question+dimension (may be unset)
            correct = q.question_dimension_ids.filtered(
                lambda qd: qd.dimension_id == line.dimension_id
            ).option_line_ids.filtered("is_correct")[:1]
            dims.append({
                "dimension": line.dimension_id.name or "",
                "selected_option": line.selected_option_id.name or "",
                "correct_option": correct.name if correct else None,
            })
        questions.append({
            "question_id": q.id,
            "title": q.name or "",
            "type": q.question_type or "text",
            "prompt": q.prompt or "",
            "description": q.description or "",
            "code_snippet": q.code_snippet or "",
            "dimensions": dims,
            "justification": resp.justification or "",
        })
    return {
        "assessment": evaluator.assessment_id.name or "",
        "candidate": evaluator.employee_id.name or "",
        "questions": questions,
    }


def score_evaluator(env, evaluator):
    """ONE Bedrock call: score every submitted response of one candidate.

    Returns list of result dicts; raises on transport/parse errors so the
    caller can mark llm_state='failed' and keep the run idempotent.
    """
    payload = build_scoring_payload(evaluator)
    if not payload["questions"]:
        return {}

    raw = _call_bedrock(
        env,
        _get_scoring_prompt(env),
        json.dumps(payload, ensure_ascii=False),
        max_tokens=4000,
        temperature=0.2,
    )
    results = _extract_json_array(raw)
    if not isinstance(results, list):
        raise ValueError(f"Scoring LLM did not return a JSON array: {str(raw)[:300]}")

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
        "etp_assessment LLM scoring: evaluator=%s questions_sent=%d results=%d",
        evaluator.id, len(payload["questions"]), len(by_qid),
    )
    return by_qid
