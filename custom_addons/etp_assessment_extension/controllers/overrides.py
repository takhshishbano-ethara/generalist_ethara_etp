"""Override approval queue (SCR-098).

Endpoints:
    GET  /api/v1/etp_assessment_ext/overrides?state=pending
    POST /api/v1/etp_assessment_ext/overrides/<id>/approve
    POST /api/v1/etp_assessment_ext/overrides/<id>/reject
"""

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    parse_json_body,
    require_assessment_manager,
    require_assessment_user,
)

OVERRIDE_STATES = ("pending", "approved", "rejected")
OVERRIDE_TYPE_LABELS = {
    "self_case": "Self-case anomaly",
    "llm_misread": "LLM mis-read",
    "scoring_dispute": "Scoring dispute",
    "technical_issue": "Technical issue",
    "other": "Other",
}


def _serialize_override(rec):
    rec.ensure_one()
    return {
        "id": rec.id,
        "code": rec.code or "",
        "assessment_id": rec.assessment_id.id if rec.assessment_id else 0,
        "assessment_name": (
            rec.assessment_id.name if rec.assessment_id else ""
        ),
        "evaluator_id": rec.evaluator_id.id if rec.evaluator_id else 0,
        "candidate_id": rec.candidate_id.id if rec.candidate_id else 0,
        "candidate_name": rec.candidate_id.name if rec.candidate_id else "",
        "candidate_employee_id": (
            rec.candidate_id.barcode
            or rec.candidate_id.identification_id
            or ""
        ) if rec.candidate_id else "",
        "override_type": rec.override_type,
        "override_type_label": OVERRIDE_TYPE_LABELS.get(
            rec.override_type, rec.override_type or "",
        ),
        "reason": rec.reason or "",
        "requester_id": rec.requester_id.id if rec.requester_id else 0,
        "requester_name": (
            rec.requester_id.name if rec.requester_id else ""
        ),
        "pl_id": rec.pl_id.id if rec.pl_id else 0,
        "pl_name": rec.pl_id.name if rec.pl_id else "",
        "state": rec.state,
        "raised_at": rec.raised_at.isoformat() if rec.raised_at else None,
        "decision_at": (
            rec.decision_at.isoformat() if rec.decision_at else None
        ),
        "decided_by_id": rec.decided_by_id.id if rec.decided_by_id else 0,
        "decided_by_name": (
            rec.decided_by_id.name if rec.decided_by_id else ""
        ),
        "decision_notes": rec.decision_notes or "",
    }


class EtpAssessmentOverrideController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/overrides",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_overrides(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        params = request.params or {}
        state = (params.get("state") or "").strip()
        domain = []
        if state:
            if state not in OVERRIDE_STATES:
                return return_Response(
                    message=(
                        f"Invalid state '{state}'. "
                        f"Allowed: {', '.join(OVERRIDE_STATES)}."
                    ),
                    status=400,
                )
            domain.append(("state", "=", state))

        Override = request.env["etp.assessment.override"].sudo()
        rows = [_serialize_override(o) for o in Override.search(domain)]
        return return_Response(
            message="OK",
            status=200,
            data={"overrides": rows, "total": len(rows)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/overrides/<int:override_id>/approve",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def approve_override(self, override_id, **kwargs):
        return self._decide(override_id, action="approve")

    @http.route(
        "/api/v1/etp_assessment_ext/overrides/<int:override_id>/reject",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def reject_override(self, override_id, **kwargs):
        return self._decide(override_id, action="reject")

    def _decide(self, override_id, action):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        override = (
            request.env["etp.assessment.override"]
            .sudo()
            .browse(override_id)
        )
        if not override.exists():
            return return_Response(message="Override not found", status=404)
        if override.state != "pending":
            return return_Response(
                message=(
                    f"Override is already {override.state}; "
                    "only pending overrides can be decided."
                ),
                status=400,
            )

        jdata = parse_json_body()
        notes = (jdata.get("decision_notes") or "").strip() or False

        try:
            if action == "approve":
                override.action_approve(decision_notes=notes)
                msg = f"Override {override.code} approved."
            else:
                override.action_reject(decision_notes=notes)
                msg = f"Override {override.code} rejected."
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )
        except Exception as exc:
            return return_Response(message=str(exc), status=500)

        return return_Response(
            message=msg,
            status=200,
            data={"override": _serialize_override(override)},
        )
