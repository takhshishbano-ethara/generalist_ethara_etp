import json
import logging

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.ethara_hrms_extension.controllers.candidates import (
    _stage_to_enum,
    _current_stage as _canon_current_stage,
    _current_status as _canon_current_status,
)

_logger = logging.getLogger(__name__)
BASE = "/api/v1/screening"

_REC_IN = {
    "shortlisted": "shortlist",
    "shortlist": "shortlist",
    "rejected": "reject",
    "reject": "reject",
    "needs_review": "needs_review",
    "maybe": "maybe",
}
_REC_OUT = {
    "shortlist": "shortlisted",
    "reject": "rejected",
    "maybe": "needs_review",
    "needs_review": "needs_review",
}


def _iso(dt):
    return dt.isoformat() if dt else None


def _serialize(applicant):
    override = None
    if applicant.resume_manual_override_reason:
        override = {
            "reason": applicant.resume_manual_override_reason,
            "at": _iso(applicant.resume_manual_override_at),
            "by": (
                applicant.resume_manual_override_by_id.name
                if applicant.resume_manual_override_by_id
                else None
            ),
        }

    payload = {}
    try:
        payload = json.loads(applicant.resume_screening_payload or "{}")
    except (ValueError, TypeError):
        payload = {}

    return {
        "candidateId": applicant.id,
        "candidateName": applicant.partner_name
        or (applicant.partner_id.name if applicant.partner_id else "")
        or "",
        "candidateCode": str(applicant.id),
        "personalEmail": applicant.email_from or "",
        "jobId": applicant.job_id.id if applicant.job_id else None,
        "positionTitle": applicant.job_id.name if applicant.job_id else None,
        "stageName": applicant.stage_id.name if applicant.stage_id else None,
        "currentStage": _canon_current_stage(applicant),
        "currentStatus": _canon_current_status(applicant),
        "resumeUrl": applicant.resume_url or None,
        "matchScore": applicant.resume_score or 0,
        "screeningScore": applicant.resume_score or 0,
        "screeningSummary": applicant.resume_summary or "",
        "recommendation": _REC_OUT.get(
            applicant.resume_recommendation, "pending",
        ),
        "manualOverride": override,
        "strengths": payload.get("strengths") or [],
        "gaps": payload.get("gaps") or [],
        "resumeUploadedAt": None,
        "lastScreenedAt": _iso(applicant.resume_screened_at),
        "createdAt": _iso(applicant.create_date),
        "llmStatus": applicant.resume_llm_status,
        "llmModel": applicant.resume_llm_model_used,
        "llmError": applicant.resume_llm_error,
        "llmPromptTokens": applicant.resume_llm_prompt_tokens or 0,
        "llmCompletionTokens": applicant.resume_llm_completion_tokens or 0,
        "llmLatencyMs": applicant.resume_llm_latency_ms or 0,
    }


def _applicant_or_404(aid):
    rec = request.env["hr.applicant"].sudo().browse(aid).exists()
    if not rec:
        return None, return_Response(
            message="Applicant not found.",
            status=404, errors=["Applicant not found."],
        )
    return rec, None


def _read_json_body():
    try:
        raw = request.httprequest.get_data(cache=False, as_text=True) or ""
        return (json.loads(raw) if raw.strip() else {}), None
    except (ValueError, TypeError) as exc:
        return None, return_Response(
            message="Request body must be valid JSON.",
            status=400, errors=[str(exc)],
        )


def _paginate(params):
    try:
        page = max(int(params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        limit = min(max(int(params.get("limit") or 25), 1), 500)
    except (TypeError, ValueError):
        limit = 25
    return page, limit, (page - 1) * limit


class EtharaScreeningApi(http.Controller):

    @http.route(
        BASE, type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def screening_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        Applicant = request.env["hr.applicant"].sudo()
        domain = [
            ("resume_url", "!=", False),
            ("job_id", "!=", False),
        ]

        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|",
                ("partner_name", "ilike", search),
                ("email_from", "ilike", search),
            ]

        rec_in = (params.get("recommendation") or "").strip().lower()
        if rec_in:
            internal = _REC_IN.get(rec_in)
            if internal:
                domain.append(("resume_recommendation", "=", internal))

        total = Applicant.search_count(domain)
        records = Applicant.search(
            domain, offset=offset, limit=limit,
            order="resume_screened_at desc nulls last, id desc",
        )
        return return_Response(
            message="OK", status=200,
            data={
                "data": [_serialize(r) for r in records],
                "total": total,
                "page": page,
                "limit": limit,
                "totalPages": (
                    (total + limit - 1) // limit if limit else 1
                ),
            },
        )

    @http.route(
        BASE + "/<int:aid>", type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def screening_detail(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        return return_Response(
            message="OK", status=200, data=_serialize(rec),
        )

    @http.route(
        BASE + "/<int:aid>/run", type="http", auth="none",
        methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def screening_run(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        try:
            rec.action_screen_resume()
        except Exception as exc:
            _logger.exception(
                "screening_run failed for hr.applicant %s", aid,
            )
            msg = str(exc) or "Screening failed"
            return return_Response(
                message=msg, status=400, errors=[msg],
            )
        rec.invalidate_recordset()
        return return_Response(
            message="Screening completed.",
            status=200, data=_serialize(rec),
        )

    @http.route(
        BASE + "/<int:aid>/override", type="http", auth="none",
        methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def screening_override(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        body, err = _read_json_body()
        if err is not None:
            return err

        rec_in = (body.get("recommendation") or "").strip().lower()
        reason = (body.get("reason") or "").strip()
        internal = _REC_IN.get(rec_in)

        if not internal:
            return return_Response(
                message=(
                    "'recommendation' must be one of "
                    "shortlisted / rejected / needs_review / maybe."
                ),
                status=400,
            )
        if not reason:
            return return_Response(
                message="'reason' is required for an override.",
                status=400,
            )

        rec.write({
            "resume_recommendation": internal,
            "resume_manual_override_reason": reason,
            "resume_manual_override_at": fields.Datetime.now(),
            "resume_manual_override_by_id": request.env.user.id,
        })
        try:
            rec._advance_stage_after_screening()
        except Exception:
            _logger.exception(
                "Post-override stage advance failed for hr.applicant %s",
                rec.id,
            )
        rec.invalidate_recordset()
        return return_Response(
            message="Screening decision updated.",
            status=200, data=_serialize(rec),
        )
