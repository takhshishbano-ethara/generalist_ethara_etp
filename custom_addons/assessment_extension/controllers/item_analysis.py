# -*- coding: utf-8 -*-
"""SCR-097 · Item Analysis (per-question cohort performance) + Distribution Drawer.

Endpoints:

  GET  /api/v1/assessment_ext/item_analysis
        ?assessment_id=N[&day=N][&task_type=...]   → KPI strip + item table

  GET  /api/v1/assessment_ext/distribution
        ?question_id=N[&assessment_id=N]           → drawer payload

  POST /api/v1/assessment_ext/flag_question
        body: {"question_id": N, "flagged": true}  → sets flagged_bad
"""
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    TASK_TYPES,
    TASK_TYPE_LABELS,
    coerce_bool,
    coerce_int,
    parse_json_body,
    question_code,
    require_monitor_user,
    role_tag,
    score_band,
    task_type_pill,
)

_logger = logging.getLogger(__name__)


# Score-distribution bins used by both the inline mini-bar (table) and the
# drawer histogram. WORKFLOW §6 + SCR-097 §3.4.2 Col G.
DISTRIBUTION_BINS = [
    {"key": "0_19", "label": "0-19", "low": 0, "high": 19, "color": "#EF4444"},
    {"key": "20_39", "label": "20-39", "low": 20, "high": 39, "color": "#F59E0B"},
    {"key": "40_59", "label": "40-59", "low": 40, "high": 59, "color": "#F59E0B"},
    {"key": "60_79", "label": "60-79", "low": 60, "high": 79, "color": "#10B981"},
    {"key": "80_100", "label": "80-100", "low": 80, "high": 100, "color": "#10B981"},
]


def _bin_for_score(score):
    if score is None:
        return None
    for b in DISTRIBUTION_BINS:
        if b["low"] <= score <= b["high"]:
            return b["key"]
    return None


def _distribution_for_question(question):
    """Return a per-bin count + the {avg, n, low_conf_pct} stat row."""
    counts = {b["key"]: 0 for b in DISTRIBUTION_BINS}
    graded = question.submission_ids.filtered(
        lambda s: s.state in ("scored", "overridden") and s.final_score is not False
    )
    for sub in graded:
        key = _bin_for_score(sub.final_score)
        if key:
            counts[key] += 1
    bins = [
        {
            **b,
            "count": counts[b["key"]],
        }
        for b in DISTRIBUTION_BINS
    ]
    return bins, len(graded)


def _flag_reason(question):
    """Return the reason a question auto-flagged-suspect, or None."""
    if not question.is_suspect:
        return None
    if question.avg_score < 25:
        return "Everyone failed — the locked key may be wrong or ambiguous."
    if question.avg_score > 95:
        return "Everyone aced — the item may be trivial or leaked."
    if question.low_confidence_pct > 40:
        return "Over 40% of responses were graded low-confidence — the model wasn't sure about this item."
    return None


def _serialize_question_row(question, pass_threshold):
    bins, n = _distribution_for_question(question)
    return {
        "id": question.id,
        "code": question_code(question),
        "task_type": task_type_pill(question.task_type),
        "day_number": question.day_number,
        "difficulty": question.difficulty or "",
        "n_responses": n,
        "avg_score": round(question.avg_score, 1) if question.avg_score else 0.0,
        "avg_score_band": score_band(question.avg_score or None, pass_threshold),
        "distribution": bins,
        "low_confidence_pct": round(question.low_confidence_pct, 1),
        "low_confidence_warn": question.low_confidence_pct > 40,
        "flagged_bad": question.flagged_bad,
        "is_suspect": question.is_suspect,
        "flag_label": (
            "Flagged - suspect" if (question.is_suspect or question.flagged_bad) else ""
        ),
        "flag_reason": _flag_reason(question),
    }


def _resolve_scope(env, assessment_id, day, task_type):
    """Return the recordset of Questions to render given the scope filters."""
    Question = env["etp.assessment.question"].sudo()
    if not assessment_id:
        return Question, None
    assessment = env["etp.assessment"].sudo().browse(assessment_id)
    if not assessment.exists():
        return Question, "Assessment not found"
    if role_tag(env) == "pl" and assessment.create_uid.id != env.user.id:
        return Question, "You do not have access to this assessment."

    question_ids = assessment.question_ids.ids
    if not question_ids:
        return Question, None
    domain = [("id", "in", question_ids)]
    if day:
        domain.append(("day_number", "=", day))
    if task_type:
        domain.append(("task_type", "=", task_type))
    return Question.search(domain), None


class ItemAnalysisController(http.Controller):
    """SCR-097 endpoints."""

    @http.route(
        "/api/v1/assessment_ext/item_analysis",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def item_analysis(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        assessment_id = coerce_int(params.get("assessment_id"))
        if not assessment_id:
            return return_Response(
                message="'assessment_id' is required.", status=400,
            )
        day = coerce_int(params.get("day"))
        task_type = (params.get("task_type") or "").strip() or None
        if task_type and task_type not in TASK_TYPES:
            return return_Response(
                message=(
                    f"Invalid task_type '{task_type}'. "
                    f"Allowed: {', '.join(TASK_TYPES)}."
                ),
                status=400,
            )

        questions, err = _resolve_scope(env, assessment_id, day, task_type)
        if err is not None:
            status = 403 if "access" in err else 404
            return return_Response(message=err, status=status)

        assessment = env["etp.assessment"].sudo().browse(assessment_id)
        pass_threshold = assessment.pass_threshold or 70

        rows = [_serialize_question_row(q, pass_threshold) for q in questions]
        # Default sort: flagged-first then avg ascending (worst items at top).
        rows.sort(
            key=lambda r: (
                not (r["is_suspect"] or r["flagged_bad"]),
                r["avg_score"],
            )
        )

        # KPI strip
        graded_count = sum(1 for r in rows if r["n_responses"] > 0)
        if graded_count:
            mean_q_score = sum(r["avg_score"] for r in rows if r["n_responses"] > 0) / graded_count
        else:
            mean_q_score = 0.0
        flagged_count = sum(1 for r in rows if r["is_suspect"] or r["flagged_bad"])
        # Mean confidence across in-scope submissions
        Submission = env["etp.assessment.submission"].sudo()
        sub_domain = [("question_id", "in", [r["id"] for r in rows])]
        all_subs = Submission.search(sub_domain)
        confidences = [s.confidence for s in all_subs if s.confidence]
        mean_conf = (sum(confidences) / len(confidences)) if confidences else 0.0

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "assessment": {
                    "id": assessment.id,
                    "name": assessment.name or "",
                    "pass_threshold": pass_threshold,
                    "period_days": assessment.period_days or 5,
                },
                "scope": {
                    "day": day,
                    "task_type": task_type,
                    "task_type_label": TASK_TYPE_LABELS.get(task_type, "All types") if task_type else "All types",
                },
                "kpis": [
                    {
                        "key": "questions_analysed",
                        "label": "Questions analysed",
                        "value": graded_count,
                        "format": "integer",
                        "band": "neutral",
                        "sub_context": (
                            f"Day {day} - graded so far" if day else "graded so far"
                        ),
                    },
                    {
                        "key": "mean_question_score",
                        "label": "Mean question score",
                        "value": round(mean_q_score, 1),
                        "format": "integer",
                        "band": (
                            "success" if mean_q_score >= pass_threshold else "warning"
                        ),
                        "sub_context": f"avg across {graded_count} questions",
                    },
                    {
                        "key": "flagged_items",
                        "label": "Flagged items",
                        "value": flagged_count,
                        "format": "integer",
                        "band": "warning" if flagged_count else "neutral",
                        "sub_context": (
                            f"{flagged_count} needs a closer look"
                            if flagged_count else "All items look healthy"
                        ),
                    },
                    {
                        "key": "mean_confidence",
                        "label": "Mean confidence",
                        "value": round(mean_conf, 2),
                        "format": "float",
                        "band": "info",
                        "sub_context": "grader certainty (0-1)",
                    },
                ],
                "rows": rows,
                "row_count": len(rows),
            },
        )

    @http.route(
        "/api/v1/assessment_ext/distribution",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def distribution(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        question_id = coerce_int(params.get("question_id"))
        if not question_id:
            return return_Response(
                message="'question_id' is required.", status=400,
            )

        question = env["etp.assessment.question"].sudo().browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        # Scope check via at least one of its assessments
        pass_threshold = 70
        assessment_id = coerce_int(params.get("assessment_id"))
        if assessment_id:
            assessment = env["etp.assessment"].sudo().browse(assessment_id)
            if assessment.exists():
                if role_tag(env) == "pl" and assessment.create_uid.id != env.user.id:
                    return return_Response(
                        message="You do not have access to this assessment.",
                        status=403,
                    )
                pass_threshold = assessment.pass_threshold or 70

        bins, n = _distribution_for_question(question)
        graded = question.submission_ids.filtered(
            lambda s: s.state in ("scored", "overridden") and s.final_score is not False
        )

        # Anonymised example answers — up to 3, varied score bands.
        examples = []
        bucket_pick = {"low": None, "mid": None, "high": None}
        for sub in graded:
            band = "high" if (sub.final_score or 0) >= pass_threshold else ("mid" if (sub.final_score or 0) >= 50 else "low")
            if not bucket_pick[band]:
                bucket_pick[band] = sub
        for band in ("low", "mid", "high"):
            sub = bucket_pick[band]
            if not sub:
                continue
            examples.append({
                "submission_id": sub.id,
                "anon_label": f"Candidate · graded {sub.final_score}",
                "final_score": sub.final_score,
                "score_band": score_band(sub.final_score, pass_threshold),
                "confidence": sub.confidence,
                "low_confidence": sub.low_confidence,
                "answer_summary": sub.answer_summary or "",
                "answer_payload": sub.parsed_answer_payload(),
            })

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "question": {
                    "id": question.id,
                    "code": question_code(question),
                    "task_type": task_type_pill(question.task_type),
                    "day_number": question.day_number,
                    "difficulty": question.difficulty or "",
                    "name": question.name or "",
                    "prompt": question.prompt or "",
                    "description": question.description or "",
                    "image_a_url": question.image_a_url or "",
                    "image_b_url": question.image_b_url or "",
                    "video_url": question.video_url or "",
                    "correct_answer": question.parsed_correct_answer(),
                    "wrong_answer": question.parsed_wrong_answer(),
                    "flagged_bad": question.flagged_bad,
                    "is_suspect": question.is_suspect,
                    "flag_reason": _flag_reason(question),
                },
                "stats": {
                    "n_responses": n,
                    "avg_score": round(question.avg_score, 1) if question.avg_score else 0.0,
                    "avg_score_band": score_band(question.avg_score or None, pass_threshold),
                    "low_confidence_pct": round(question.low_confidence_pct, 1),
                    "low_confidence_warn": question.low_confidence_pct > 40,
                    "pass_threshold": pass_threshold,
                },
                "distribution": bins,
                "examples": examples,
            },
        )

    @http.route(
        "/api/v1/assessment_ext/flag_question",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def flag_question(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        body = parse_json_body()
        question_id = coerce_int(body.get("question_id"))
        flagged = coerce_bool(body.get("flagged"), True)
        if not question_id:
            return return_Response(
                message="'question_id' is required.", status=400,
            )

        question = env["etp.assessment.question"].sudo().browse(question_id)
        if not question.exists():
            return return_Response(message="Question not found", status=404)

        # Scope check via a referenced assessment if provided
        assessment_id = coerce_int(body.get("assessment_id"))
        if assessment_id:
            assessment = env["etp.assessment"].sudo().browse(assessment_id)
            if assessment.exists():
                if role_tag(env) == "pl" and assessment.create_uid.id != env.user.id:
                    return return_Response(
                        message="You do not have access to this assessment.",
                        status=403,
                    )

        question.write({"flagged_bad": bool(flagged)})
        return return_Response(
            message=(
                f"{question.code or 'Question'} flagged for regeneration."
                if flagged else
                f"{question.code or 'Question'} flag cleared."
            ),
            status=200,
            data={
                "question_id": question.id,
                "code": question_code(question),
                "flagged_bad": question.flagged_bad,
            },
        )
