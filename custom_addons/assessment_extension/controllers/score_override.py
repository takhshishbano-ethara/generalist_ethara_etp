# -*- coding: utf-8 -*-
"""MOD-Score-Override · Score Override (PL / HR / CTO).

Endpoints:

  GET  /api/v1/assessment_ext/override_context?submission_id=N
        → the panel's prefill payload (LLM score, confidence, rationale,
          recompute preview, self-case guard verdict)

  POST /api/v1/assessment_ext/override
        body: {
          submission_id, new_score, reason, note?, item_result?,
        }
        → either commits the override directly (HR/CTO, or PL on a
          non-self-case) or creates a pending override.request that lands
          in the CTO inbox (PL on a self-case, §6.5).

WORKFLOW §6.4–6.5, §12.4, §18.
"""
from datetime import datetime

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    OVERRIDE_REASONS,
    assessment_code,
    coerce_int,
    confidence_band,
    employee_card,
    iso_or_none,
    parse_json_body,
    question_code,
    reason_label,
    require_monitor_user,
    role_tag,
    score_band,
    submission_code,
    task_type_pill,
)


def _recompute_preview(submission, new_score):
    """Return the §3.7 recompute preview block (day mean / overall mean before vs after)."""
    sub = submission
    evaluator = sub.evaluator_id
    day = sub.day_session_id

    # Compute the new day mean if we replaced this submission's final_score.
    day_graded = day.submission_ids.filtered(
        lambda s: s.state in ("scored", "overridden") and s.final_score is not False
    ) if day else sub.browse([])
    if day_graded:
        cur_day_sum = sum(s.final_score or 0 for s in day_graded)
        new_day_sum = cur_day_sum - (sub.final_score or 0) + (new_score or 0)
        cur_day_mean = cur_day_sum / len(day_graded)
        new_day_mean = new_day_sum / len(day_graded)
    else:
        cur_day_mean = new_day_mean = None

    overall_graded = evaluator.submission_ids.filtered(
        lambda s: s.state in ("scored", "overridden") and s.final_score is not False
    )
    if overall_graded:
        cur_overall_sum = sum(s.final_score or 0 for s in overall_graded)
        new_overall_sum = cur_overall_sum - (sub.final_score or 0) + (new_score or 0)
        cur_overall_mean = cur_overall_sum / len(overall_graded)
        new_overall_mean = new_overall_sum / len(overall_graded)
    else:
        cur_overall_mean = new_overall_mean = None

    return {
        "day_mean_before": round(cur_day_mean, 1) if cur_day_mean is not None else None,
        "day_mean_after": round(new_day_mean, 1) if new_day_mean is not None else None,
        "overall_mean_before": round(cur_overall_mean, 1) if cur_overall_mean is not None else None,
        "overall_mean_after": round(new_overall_mean, 1) if new_overall_mean is not None else None,
    }


def _serialize_context(submission, actor_user):
    sub = submission
    a = sub.assessment_id
    pt = a.pass_threshold or 70
    delta_threshold = a.override_delta_threshold or 10
    is_self_case = sub.is_self_case(sub.employee_id, actor_user)
    role = role_tag(request.env)
    can_commit_directly = role in ("hr", "cto") or not is_self_case

    return {
        "submission": {
            "id": sub.id,
            "code": submission_code(sub),
            "state": sub.state,
            "llm_score": sub.llm_score,
            "final_score": sub.final_score if sub.state in ("scored", "overridden") else sub.llm_score,
            "confidence": sub.confidence,
            "confidence_band": confidence_band(sub.confidence),
            "low_confidence": sub.low_confidence,
            "llm_rationale": sub.llm_rationale or "",
            "item_result": sub.item_result or "auto",
            "score_band": score_band(sub.llm_score, pt),
        },
        "question": {
            "id": sub.question_id.id,
            "code": question_code(sub.question_id),
            "task_type": task_type_pill(sub.task_type),
            "day_number": sub.day_number,
            "name": sub.question_id.name or "",
            "prompt": sub.question_id.prompt or "",
        },
        "candidate": employee_card(sub.employee_id),
        "assessment": {
            "id": a.id,
            "code": assessment_code(a),
            "name": a.name or "",
            "pass_threshold": pt,
            "override_delta_threshold": delta_threshold,
        },
        "policy": {
            "min": 0,
            "max": 100,
            "default_value": sub.final_score if sub.state in ("scored", "overridden") else (sub.llm_score or 0),
            "delta_threshold": delta_threshold,
            "reason_required_when_large_delta": True,
            "reasons": [
                {"key": k, "label": reason_label(k)} for k in OVERRIDE_REASONS
            ],
        },
        "guard": {
            "is_self_case": is_self_case,
            "can_commit_directly": can_commit_directly,
            "escalation_message": (
                "This is your direct report - the override goes to the CTO for approval."
                if is_self_case and role == "pl" else ""
            ),
            "primary_action_label": (
                "Send to CTO" if is_self_case and role == "pl" else "Confirm override"
            ),
        },
    }


class ScoreOverrideController(http.Controller):
    """MOD-Score-Override endpoints."""

    @http.route(
        "/api/v1/assessment_ext/override_context",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def override_context(self, **kwargs):
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
        sub = Submission.browse(submission_id)
        if not sub.exists():
            return return_Response(message="Submission not found", status=404)
        a = sub.assessment_id
        if role_tag(env) == "pl" and a.create_uid.id != env.user.id:
            return return_Response(
                message="You do not have access to this submission.",
                status=403,
            )

        context = _serialize_context(sub, env.user)
        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                **context,
            },
        )

    @http.route(
        "/api/v1/assessment_ext/override_preview",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def override_preview(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        submission_id = coerce_int(params.get("submission_id"))
        new_score = coerce_int(params.get("new_score"))
        if not submission_id or new_score is None:
            return return_Response(
                message="'submission_id' and 'new_score' are required.", status=400,
            )
        if new_score < 0 or new_score > 100:
            return return_Response(
                message="new_score must be 0-100.", status=400,
            )

        sub = env["etp.assessment.submission"].sudo().browse(submission_id)
        if not sub.exists():
            return return_Response(message="Submission not found", status=404)

        delta = new_score - (sub.llm_score or 0)
        delta_threshold = sub.assessment_id.override_delta_threshold or 10
        preview = _recompute_preview(sub, new_score)
        return return_Response(
            message="OK",
            status=200,
            data={
                "delta": delta,
                "delta_band": (
                    "neutral" if delta == 0 else (
                        "warning" if abs(delta) > delta_threshold else "info"
                    )
                ),
                "reason_required": abs(delta) > delta_threshold,
                "recompute": preview,
            },
        )

    @http.route(
        "/api/v1/assessment_ext/override",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def override(self, **kwargs):
        forbidden = require_monitor_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        body = parse_json_body()
        submission_id = coerce_int(body.get("submission_id"))
        new_score = coerce_int(body.get("new_score"))
        reason = (body.get("reason") or "").strip() or None
        note = (body.get("note") or "").strip() or None
        item_result = (body.get("item_result") or "").strip() or None

        if not submission_id:
            return return_Response(
                message="'submission_id' is required.", status=400,
            )
        if new_score is None:
            return return_Response(
                message="'new_score' is required.", status=400,
            )
        if new_score < 0 or new_score > 100:
            return return_Response(
                message="new_score must be 0-100.", status=400,
            )
        if reason and reason not in OVERRIDE_REASONS:
            return return_Response(
                message=(
                    f"Invalid reason '{reason}'. Allowed: {', '.join(OVERRIDE_REASONS)}."
                ),
                status=400,
            )
        if item_result and item_result not in ("auto", "pass", "fail"):
            return return_Response(
                message="item_result must be one of 'auto', 'pass', 'fail'.",
                status=400,
            )

        Submission = env["etp.assessment.submission"].sudo()
        sub = Submission.browse(submission_id)
        if not sub.exists():
            return return_Response(message="Submission not found", status=404)
        a = sub.assessment_id
        if role_tag(env) == "pl" and a.create_uid.id != env.user.id:
            return return_Response(
                message="You do not have access to this submission.",
                status=403,
            )

        delta_threshold = a.override_delta_threshold or 10
        delta = new_score - (sub.llm_score or 0)
        if abs(delta) > delta_threshold and not reason:
            return return_Response(
                message="A reason is required for a change this large.",
                status=400,
            )
        if reason == "other" and not note:
            return return_Response(
                message="A short note is required when reason is 'Other'.",
                status=400,
            )

        is_self_case = sub.is_self_case(sub.employee_id, env.user)
        role = role_tag(env)

        # §6.5: PL on own report's self-case → escalate to CTO; everyone else commits.
        if role == "pl" and is_self_case:
            existing = env["etp.assessment.override.request"].sudo().search(
                [
                    ("submission_id", "=", sub.id),
                    ("state", "=", "pending"),
                ],
                limit=1,
            )
            if existing:
                return return_Response(
                    message=(
                        f"A pending override request already exists for {sub.question_id.code or 'this question'}."
                    ),
                    status=409,
                    data={"request_id": existing.id, "code": existing.code or ""},
                )

            try:
                record = env["etp.assessment.override.request"].sudo().create({
                    "submission_id": sub.id,
                    "llm_score": sub.llm_score,
                    "requested_score": new_score,
                    "requested_reason": reason,
                    "requested_note": note,
                    "item_result": item_result or "auto",
                    "requesting_user_id": env.user.id,
                    "requested_at": fields.Datetime.now(),
                })
            except (UserError, ValidationError) as exc:
                return return_Response(
                    message=str(exc.args[0] if exc.args else exc), status=400,
                )

            return return_Response(
                message=(
                    "Sent to the CTO — they will review your override request for "
                    f"{sub.question_id.code or 'this question'}."
                ),
                status=200,
                data={
                    "outcome": "escalated_to_cto",
                    "request_id": record.id,
                    "request_code": record.code or "",
                    "row_badge": {
                        "label": "Override pending CTO",
                        "bg": "#FFF7ED",
                        "text": "#C2410C",
                        "dot": "#F59E0B",
                    },
                },
            )

        # Direct commit (HR / CTO, or PL on someone outside their team).
        try:
            sub.apply_override(
                new_score=new_score,
                reason=reason,
                note=note,
                item_result=item_result or "auto",
                actor_user=env.user,
            )
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        return return_Response(
            message=(
                f"Score overridden — {sub.question_id.code or 'question'} "
                f"set to {new_score} for {sub.employee_id.name}."
            ),
            status=200,
            data={
                "outcome": "committed",
                "submission": {
                    "id": sub.id,
                    "code": submission_code(sub),
                    "state": sub.state,
                    "llm_score": sub.llm_score,
                    "override_score": sub.override_score,
                    "final_score": sub.final_score,
                    "override_by": sub.override_by.name if sub.override_by else None,
                    "override_at": iso_or_none(sub.override_at),
                    "override_reason": sub.override_reason,
                    "override_reason_label": reason_label(sub.override_reason),
                },
                "row_badge": {
                    "label": "Overridden",
                    "bg": "#F0EDFF",
                    "text": "#3927BF",
                    "dot": "#3927BF",
                    "struck_value": sub.llm_score,
                    "new_value": sub.override_score,
                },
                "recompute": _recompute_preview(sub, sub.final_score),
            },
        )
