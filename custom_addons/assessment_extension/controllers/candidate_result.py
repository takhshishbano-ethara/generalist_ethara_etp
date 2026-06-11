# -*- coding: utf-8 -*-
"""SCR-096 · Candidate Result drill-in.

Two endpoints, mirroring the spec's two coupled surfaces:

  GET /api/v1/assessment_ext/candidate_result
        ?assessment_id=N&candidate_id=N            → drawer-index payload
  GET /api/v1/assessment_ext/question_review
        ?submission_id=N                           → wide REVIEW payload
"""
from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    assessment_code,
    confidence_band,
    coerce_int,
    employee_card,
    iso_or_none,
    question_code,
    reason_label,
    require_monitor_user,
    role_tag,
    score_band,
    status_pill_recipe,
    submission_code,
    task_type_pill,
)


def _serialize_question_row(submission, pass_threshold):
    """One row in the per-day question list (SCR-096 §3.3)."""
    sub = submission
    final = sub.final_score if sub.state in ("scored", "overridden") else None
    return {
        "id": sub.id,
        "submission_code": submission_code(sub),
        "question_id": sub.question_id.id,
        "question_code": question_code(sub.question_id),
        "task_type": task_type_pill(sub.task_type),
        "answer_summary": sub.answer_summary or "",
        "state": sub.state,
        "status_pill": status_pill_recipe(sub.state),
        "llm_score": sub.llm_score,
        "override_score": sub.override_score if sub.state == "overridden" else None,
        "final_score": final,
        "final_score_band": score_band(final, pass_threshold),
        "confidence": sub.confidence,
        "low_confidence": sub.low_confidence,
        "is_overridden": sub.state == "overridden",
        "override_by": sub.override_by.name if sub.override_by else None,
        "override_at": iso_or_none(sub.override_at),
    }


def _serialize_day_row(day, pass_threshold):
    """One row in the drawer body (SCR-096 §3.2)."""
    questions = day.submission_ids.sorted(key=lambda s: (s.question_id.sequence, s.question_id.id))
    return {
        "id": day.id,
        "day_number": day.day_number,
        "day_date": day.day_date.isoformat() if day.day_date else None,
        "status": day.status,
        "status_pill": status_pill_recipe(day.status),
        "submitted_count": day.submitted_count,
        "questions_per_day": day.questions_per_day,
        "day_mean": round(day.day_mean, 1) if day.day_mean else None,
        "day_mean_band": score_band(day.day_mean if day.submitted_count else None, pass_threshold),
        "type_means": [
            {"task_type": "eval_compare", "mean": round(day.eval_mean, 1) if day.eval_mean else None, "band": score_band(day.eval_mean or None, pass_threshold)},
            {"task_type": "prompt_writing", "mean": round(day.prompt_mean, 1) if day.prompt_mean else None, "band": score_band(day.prompt_mean or None, pass_threshold)},
            {"task_type": "bbox_labeling", "mean": round(day.bbox_mean, 1) if day.bbox_mean else None, "band": score_band(day.bbox_mean or None, pass_threshold)},
        ],
        "questions": [_serialize_question_row(s, pass_threshold) for s in questions],
    }


class CandidateResultController(http.Controller):
    """SCR-096 endpoints."""

    @http.route(
        "/api/v1/assessment_ext/candidate_result",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def candidate_result(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        assessment_id = coerce_int(params.get("assessment_id"))
        candidate_id = coerce_int(params.get("candidate_id"))
        if not assessment_id or not candidate_id:
            return return_Response(
                message="'assessment_id' and 'candidate_id' are required.",
                status=400,
            )

        Assessment = env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)
        if role_tag(env) == "pl" and assessment.create_uid.id != env.user.id:
            return return_Response(
                message="You do not have access to this assessment.",
                status=403,
            )

        evaluator = env["etp.assessment.evaluator"].sudo().search(
            [
                ("assessment_id", "=", assessment.id),
                "|",
                ("employee_id", "=", candidate_id),
                ("id", "=", candidate_id),
            ],
            limit=1,
        )
        if not evaluator:
            return return_Response(
                message="Candidate is not assigned to this assessment.",
                status=404,
            )

        pass_threshold = assessment.pass_threshold or 70

        # Header / summary
        employee = evaluator.employee_id
        emp_card = employee_card(employee)
        overall_status = "at_risk" if evaluator.is_at_risk else (
            "passed" if evaluator.overall_mean >= pass_threshold else (
                "in_progress" if evaluator.submitted_total < evaluator.submissions_expected else "failed"
            )
        )

        # Day breakdown (always render the configured number of days; missing
        # day rows render with status='locked' / mean=None on the client)
        existing_days = {d.day_number: d for d in evaluator.day_session_ids}
        day_rows = []
        for n in range(1, (assessment.period_days or 5) + 1):
            day = existing_days.get(n)
            if day:
                day_rows.append(_serialize_day_row(day, pass_threshold))
            else:
                day_rows.append({
                    "id": None,
                    "day_number": n,
                    "day_date": None,
                    "status": "locked",
                    "status_pill": status_pill_recipe("locked"),
                    "submitted_count": 0,
                    "questions_per_day": assessment.questions_per_day,
                    "day_mean": None,
                    "day_mean_band": "muted",
                    "type_means": [
                        {"task_type": t, "mean": None, "band": "muted"}
                        for t in ("eval_compare", "prompt_writing", "bbox_labeling")
                    ],
                    "questions": [],
                })

        # Self-case guard for the footer override button
        is_self_case = env["etp.assessment.submission"].is_self_case(employee, env.user)
        can_override_directly = role_tag(env) in ("hr", "cto") or not is_self_case

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "assessment": {
                    "id": assessment.id,
                    "code": assessment_code(assessment),
                    "name": assessment.name or "",
                    "cohort_label": assessment.cohort_label or "",
                    "pass_threshold": pass_threshold,
                    "period_days": assessment.period_days or 5,
                    "questions_per_day": assessment.questions_per_day or 25,
                    "monitor_state": assessment.monitor_state,
                    "monitor_state_pill": status_pill_recipe(assessment.monitor_state),
                    "start_date": iso_or_none(assessment.start_date),
                    "end_date": iso_or_none(assessment.end_date),
                },
                "candidate": {
                    **(emp_card or {}),
                    "evaluator_id": evaluator.id,
                    "cohort_batch": evaluator.cohort_batch or "",
                    "last_activity_at": iso_or_none(evaluator.last_activity_at),
                    "overall_mean": round(evaluator.overall_mean, 1) if evaluator.overall_mean else None,
                    "overall_mean_band": score_band(
                        evaluator.overall_mean if evaluator.submitted_total else None,
                        pass_threshold,
                    ),
                    "submitted_total": evaluator.submitted_total,
                    "submissions_expected": evaluator.submissions_expected,
                    "review_count": evaluator.review_count,
                    "is_at_risk": evaluator.is_at_risk,
                    "status": overall_status,
                    "status_pill": status_pill_recipe(overall_status),
                    "can_override_directly": can_override_directly,
                    "is_self_case": is_self_case,
                },
                "days": day_rows,
            },
        )

    @http.route(
        "/api/v1/assessment_ext/question_review",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def question_review(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        submission_id = coerce_int(params.get("submission_id"))
        if not submission_id:
            return return_Response(
                message="'submission_id' is required.", status=400,
            )

        Submission = env["etp.assessment.submission"].sudo()
        submission = Submission.browse(submission_id)
        if not submission.exists():
            return return_Response(message="Submission not found", status=404)
        assessment = submission.assessment_id
        if role_tag(env) == "pl" and assessment.create_uid.id != env.user.id:
            return return_Response(
                message="You do not have access to this submission.",
                status=403,
            )

        pass_threshold = assessment.pass_threshold or 70
        question = submission.question_id

        # Adjacent submissions for the REVIEW prev/next stepper
        siblings = Submission.search(
            [
                ("evaluator_id", "=", submission.evaluator_id.id),
                ("day_session_id", "=", submission.day_session_id.id),
            ],
            order="question_id, id",
        )
        ordered_ids = siblings.ids
        prev_id = next_id = None
        try:
            idx = ordered_ids.index(submission.id)
            prev_id = ordered_ids[idx - 1] if idx > 0 else None
            next_id = ordered_ids[idx + 1] if idx + 1 < len(ordered_ids) else None
        except ValueError:
            pass

        is_self_case = Submission.is_self_case(submission.employee_id, env.user)
        can_override_directly = role_tag(env) in ("hr", "cto") or not is_self_case

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "submission": {
                    "id": submission.id,
                    "code": submission_code(submission),
                    "state": submission.state,
                    "answer_payload": submission.parsed_answer_payload(),
                    "answer_summary": submission.answer_summary or "",
                    "llm_score": submission.llm_score,
                    "override_score": submission.override_score if submission.state == "overridden" else None,
                    "final_score": submission.final_score if submission.state in ("scored", "overridden") else None,
                    "final_score_band": score_band(
                        submission.final_score if submission.state in ("scored", "overridden") else None,
                        pass_threshold,
                    ),
                    "confidence": submission.confidence,
                    "confidence_band": confidence_band(submission.confidence),
                    "low_confidence": submission.low_confidence,
                    "llm_rationale": submission.llm_rationale or "",
                    "sub_scores": submission.parsed_sub_scores(),
                    "item_result": submission.item_result,
                    "override_by": submission.override_by.name if submission.override_by else None,
                    "override_at": iso_or_none(submission.override_at),
                    "override_reason": submission.override_reason,
                    "override_reason_label": reason_label(submission.override_reason),
                    "override_note": submission.override_note or "",
                    "submitted_at": iso_or_none(submission.submitted_at),
                    "scored_at": iso_or_none(submission.scored_at),
                    "day_number": submission.day_number,
                    "day_date": submission.day_session_id.day_date.isoformat() if submission.day_session_id.day_date else None,
                },
                "question": {
                    "id": question.id,
                    "code": question_code(question),
                    "task_type": task_type_pill(submission.task_type),
                    "difficulty": question.difficulty or "",
                    "day_number": question.day_number,
                    "name": question.name or "",
                    "prompt": question.prompt or "",
                    "description": question.description or "",
                    "image_a_url": question.image_a_url or "",
                    "image_b_url": question.image_b_url or "",
                    "video_url": question.video_url or "",
                    "correct_answer": question.parsed_correct_answer(),
                    "wrong_answer": question.parsed_wrong_answer(),
                    "flagged_bad": question.flagged_bad,
                },
                "candidate": employee_card(submission.employee_id),
                "assessment": {
                    "id": assessment.id,
                    "code": assessment_code(assessment),
                    "name": assessment.name or "",
                    "pass_threshold": pass_threshold,
                    "override_delta_threshold": assessment.override_delta_threshold or 10,
                },
                "neighbors": {
                    "prev_submission_id": prev_id,
                    "next_submission_id": next_id,
                },
                "permissions": {
                    "is_self_case": is_self_case,
                    "can_override_directly": can_override_directly,
                    "can_open_override": True,
                },
            },
        )
