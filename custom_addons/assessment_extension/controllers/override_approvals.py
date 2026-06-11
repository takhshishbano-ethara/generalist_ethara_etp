# -*- coding: utf-8 -*-
"""SCR-098 · CTO Override Approvals (self-case escalation inbox).

Endpoints:

  GET  /api/v1/assessment_ext/override_approvals               → pending list
  POST /api/v1/assessment_ext/override_approvals/<id>/approve  → commit override
  POST /api/v1/assessment_ext/override_approvals/<id>/reject   → reject + audit
"""
from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    coerce_int,
    employee_card,
    iso_or_none,
    parse_json_body,
    question_code,
    reason_label,
    require_cto,
    role_tag,
    score_band,
    submission_code,
    task_type_pill,
)


def _serialize_request(req, pass_threshold=70):
    sub = req.submission_id
    pl_employee = req.requesting_user_id.employee_id
    return {
        "id": req.id,
        "code": req.code or "",
        "state": req.state,
        "requested_at": iso_or_none(req.requested_at),
        "requesting_pl": {
            "user_id": req.requesting_user_id.id,
            "name": req.requesting_user_id.name or "",
            "email": req.requesting_user_id.login or "",
            "employee": employee_card(pl_employee) if pl_employee else None,
        },
        "candidate": employee_card(req.candidate_employee_id),
        "assessment": {
            "id": req.assessment_id.id if req.assessment_id else None,
            "name": req.assessment_id.name or "" if req.assessment_id else "",
            "pass_threshold": (req.assessment_id.pass_threshold or 70) if req.assessment_id else 70,
        },
        "question": {
            "id": req.question_id.id if req.question_id else None,
            "code": question_code(req.question_id) if req.question_id else "",
            "task_type": task_type_pill(req.question_id.task_type if req.question_id else None),
            "day_number": req.question_id.day_number if req.question_id else None,
        },
        "submission": {
            "id": sub.id if sub else None,
            "code": submission_code(sub) if sub else "",
            "current_llm_score": sub.llm_score if sub else None,
            "current_final_score": sub.final_score if sub else None,
            "confidence": sub.confidence if sub else None,
            "low_confidence": sub.low_confidence if sub else False,
        },
        "score_change": {
            "from": req.llm_score,
            "to": req.requested_score,
            "delta": (req.requested_score or 0) - (req.llm_score or 0),
            "to_band": score_band(req.requested_score, pass_threshold),
        },
        "reason": {
            "key": req.requested_reason,
            "label": reason_label(req.requested_reason),
            "note": req.requested_note or "",
        },
        "item_result": req.item_result or "auto",
        "decided_by": req.decided_by.name if req.decided_by else None,
        "decided_at": iso_or_none(req.decided_at),
        "decision_note": req.decision_note or "",
    }


class OverrideApprovalsController(http.Controller):
    """SCR-098 endpoints."""

    @http.route(
        "/api/v1/assessment_ext/override_approvals",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_pending(self, **kwargs):
        forbidden = require_cto()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        state = (params.get("state") or "pending").strip()
        if state not in ("pending", "approved", "rejected", "all"):
            return return_Response(
                message=(
                    "Invalid state. Allowed: pending, approved, rejected, all."
                ),
                status=400,
            )

        Req = env["etp.assessment.override.request"].sudo()
        domain = [] if state == "all" else [("state", "=", state)]
        records = Req.search(domain, order="requested_at desc")
        rows = [_serialize_request(r) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": role_tag(env),
                "info_banner": {
                    "icon": "ShieldCheck",
                    "text": (
                        f"{len(rows)} pending requests · "
                        "escalated because the candidate reports to the requesting PL."
                    ),
                } if state == "pending" else None,
                "rows": rows,
                "row_count": len(rows),
            },
        )

    @http.route(
        "/api/v1/assessment_ext/override_approvals/<int:request_id>/approve",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def approve(self, request_id, **kwargs):
        forbidden = require_cto()
        if forbidden is not None:
            return forbidden

        env = request.env
        body = parse_json_body()
        note = (body.get("note") or "").strip() or None

        record = env["etp.assessment.override.request"].sudo().browse(request_id)
        if not record.exists():
            return return_Response(message="Override request not found", status=404)
        try:
            record.action_approve(note=note)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        sub = record.submission_id
        return return_Response(
            message=(
                f"Override approved — {sub.question_id.code or 'question'} "
                f"set to {record.requested_score} for {sub.employee_id.name}."
            ),
            status=200,
            data={
                "request": _serialize_request(record),
                "submission": {
                    "id": sub.id,
                    "code": submission_code(sub),
                    "final_score": sub.final_score,
                    "state": sub.state,
                },
            },
        )

    @http.route(
        "/api/v1/assessment_ext/override_approvals/<int:request_id>/reject",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def reject(self, request_id, **kwargs):
        forbidden = require_cto()
        if forbidden is not None:
            return forbidden

        env = request.env
        body = parse_json_body()
        note = (body.get("note") or "").strip() or None

        record = env["etp.assessment.override.request"].sudo().browse(request_id)
        if not record.exists():
            return return_Response(message="Override request not found", status=404)
        try:
            record.action_reject(note=note)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        return return_Response(
            message=(
                f"Override rejected — {record.submission_id.question_id.code or 'question'} "
                f"keeps its LLM score."
            ),
            status=200,
            data={"request": _serialize_request(record)},
        )
