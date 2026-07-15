import csv
import io
import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)
BASE = "/api/v1/candidates"

_REC_OUT = {
    "shortlist": "shortlisted",
    "reject": "rejected",
    "maybe": "needs_review",
    "needs_review": "needs_review",
}

_WRITABLE_FIELDS = {
    "fullName":               ("partner_name", str),
    "priorityScore":          ("priority_score", float),
    "onHold":                 ("on_hold", bool),
    "isReapplicationBlocked": ("is_reapplication_blocked", bool),
    "blacklistReason":        ("blacklist_reason", str),
    "email":                  ("email_from", str),
    "phone":                  ("partner_phone", str),
}


def _iso(dt):
    return dt.isoformat() if dt else None


def _current_status(applicant):
    if applicant.refuse_reason_id:
        return "Rejected"
    if applicant.is_reapplication_blocked:
        return "Blacklisted"
    if applicant.on_hold:
        return "On Hold"
    return applicant.stage_id.name if applicant.stage_id else None


def _serialize(applicant):
    payload = {}
    try:
        payload = json.loads(applicant.resume_screening_payload or "{}")
    except (ValueError, TypeError):
        payload = {}

    override = None
    if applicant.resume_manual_override_reason:
        override = {
            "reason": applicant.resume_manual_override_reason,
            "at": _iso(applicant.resume_manual_override_at),
            "by": (
                applicant.resume_manual_override_by_id.name
                if applicant.resume_manual_override_by_id else None
            ),
        }

    return {
        "id":                       applicant.id,
        "candidateId":              applicant.id,
        "fullName":                 applicant.partner_name
                                    or (applicant.partner_id.name if applicant.partner_id else "")
                                    or "",
        "candidateCode":            str(applicant.id),
        "personalEmail":            applicant.email_from or "",
        "phone":                    applicant.partner_phone or "",
        "jobId":                    applicant.job_id.id if applicant.job_id else None,
        "positionId":               applicant.job_id.id if applicant.job_id else None,
        "positionTitle":            applicant.job_id.name if applicant.job_id else None,
        "position": {
            "id":   applicant.job_id.id,
            "title": applicant.job_id.name,
        } if applicant.job_id else None,
        "stageId":                  applicant.stage_id.id if applicant.stage_id else None,
        "currentStage":             applicant.stage_id.name if applicant.stage_id else None,
        "currentStatus":            _current_status(applicant),
        "priorityScore":            applicant.priority_score or 0,
        "onHold":                   bool(applicant.on_hold),
        "isReapplicationBlocked":   bool(applicant.is_reapplication_blocked),
        "blacklistReason":          applicant.blacklist_reason or "",
        "resumeUrl":                applicant.resume_url or None,
        "resumeScore":              applicant.resume_score or 0,
        "screeningScore":           applicant.resume_score or 0,
        "matchScore":               applicant.resume_score or 0,
        "resumeSummary":            applicant.resume_summary or "",
        "screeningSummary":         applicant.resume_summary or "",
        "resumeRecommendation":     _REC_OUT.get(applicant.resume_recommendation, "pending"),
        "recommendation":           _REC_OUT.get(applicant.resume_recommendation, "pending"),
        "screeningPayload":         payload,
        "manualOverride":           override,
        "strengths":                payload.get("strengths") or [],
        "gaps":                     payload.get("gaps") or [],
        "llmStatus":                applicant.resume_llm_status,
        "llmModel":                 applicant.resume_llm_model_used,
        "llmError":                 applicant.resume_llm_error,
        "llmPromptTokens":          applicant.resume_llm_prompt_tokens or 0,
        "llmCompletionTokens":      applicant.resume_llm_completion_tokens or 0,
        "llmLatencyMs":             applicant.resume_llm_latency_ms or 0,
        "lastScreenedAt":           _iso(applicant.resume_screened_at),
        "createdAt":                _iso(applicant.create_date),
        "updatedAt":                _iso(applicant.write_date),
        "lastActivityAt":           _iso(applicant.write_date),
        "active":                   bool(applicant.active),
    }


def _applicant_or_404(aid):
    rec = request.env["hr.applicant"].sudo().with_context(
        active_test=False,
    ).browse(aid).exists()
    if not rec:
        return None, return_Response(
            message="Candidate not found.",
            status=404, errors=["Candidate not found."],
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


def _build_domain(params, active_test=True):
    domain = [("active", "=", True)] if active_test else []

    search = (params.get("search") or "").strip()
    if search:
        domain += [
            "|",
            ("partner_name", "ilike", search),
            ("email_from", "ilike", search),
        ]

    for pkey, dfield in (
        ("positionId", "job_id"),
        ("jobId", "job_id"),
        ("stageId", "stage_id"),
    ):
        val = params.get(pkey)
        if val:
            try:
                domain.append((dfield, "=", int(val)))
            except (TypeError, ValueError):
                pass

    current_stage = (params.get("currentStage") or "").strip()
    if current_stage:
        domain.append(("stage_id.name", "ilike", current_stage))

    on_hold = params.get("onHold")
    if on_hold in ("1", "true", "True", True):
        domain.append(("on_hold", "=", True))

    blocked = params.get("isReapplicationBlocked")
    if blocked in ("1", "true", "True", True):
        domain.append(("is_reapplication_blocked", "=", True))

    return domain


class EtharaCandidatesApi(http.Controller):

    @http.route(
        BASE, type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def candidates_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)
        Applicant = request.env["hr.applicant"].sudo()
        domain = _build_domain(params, active_test=True)
        total = Applicant.search_count(domain)
        records = Applicant.search(
            domain, offset=offset, limit=limit,
            order="write_date desc, create_date desc, id desc",
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
        BASE + "/stats", type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def candidates_stats(self, **kwargs):
        Applicant = request.env["hr.applicant"].sudo()
        base = [("active", "=", True)]
        stats = {
            "total":       Applicant.search_count(base),
            "onHold":      Applicant.search_count(base + [("on_hold", "=", True)]),
            "blacklisted": Applicant.search_count(
                base + [("is_reapplication_blocked", "=", True)],
            ),
            "screened":    Applicant.search_count(
                base + [("resume_llm_status", "=", "completed")],
            ),
            "shortlisted": Applicant.search_count(
                base + [("resume_recommendation", "=", "shortlist")],
            ),
            "rejected":    Applicant.search_count(
                base + [("resume_recommendation", "=", "reject")],
            ),
            "withResume":  Applicant.search_count(
                base + [("resume_url", "!=", False)],
            ),
            "withJob":     Applicant.search_count(
                base + [("job_id", "!=", False)],
            ),
        }
        return return_Response(message="OK", status=200, data=stats)

    @http.route(
        BASE + "/export", type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def candidates_export(self, **kwargs):
        params = request.params or {}
        Applicant = request.env["hr.applicant"].sudo()
        domain = _build_domain(params, active_test=True)
        records = Applicant.search(
            domain, limit=10000,
            order="write_date desc, id desc",
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "fullName", "email", "phone", "jobId", "positionTitle",
            "stage", "status", "resumeScore", "recommendation",
            "priorityScore", "onHold", "blacklisted", "createdAt", "updatedAt",
        ])
        for r in records:
            writer.writerow([
                r.id,
                r.partner_name or "",
                r.email_from or "",
                r.partner_phone or "",
                r.job_id.id if r.job_id else "",
                r.job_id.name if r.job_id else "",
                r.stage_id.name if r.stage_id else "",
                _current_status(r) or "",
                r.resume_score or 0,
                _REC_OUT.get(r.resume_recommendation, "pending"),
                r.priority_score or 0,
                bool(r.on_hold),
                bool(r.is_reapplication_blocked),
                _iso(r.create_date) or "",
                _iso(r.write_date) or "",
            ])
        return request.make_response(
            buf.getvalue(),
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition",
                 'attachment; filename="candidates-export.csv"'),
            ],
        )

    @http.route(
        BASE + "/employee-codes/backfill-signed",
        type="http", auth="none", methods=["POST"],
        csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidates_backfill_employee_codes(self, **kwargs):
        Employee = request.env["hr.employee"].sudo()
        Applicant = request.env["hr.applicant"].sudo()
        updated = 0
        skipped = 0
        applicants = Applicant.search([
            ("emp_id", "!=", False),
            ("employee_id", "!=", False),
        ]) if "emp_id" in Applicant._fields else Applicant.browse([])
        for a in applicants:
            emp = a.employee_id if "employee_id" in a._fields else Employee
            if emp and "employee_code" in emp._fields and not emp.employee_code:
                emp.write({"employee_code": "GRP-BACKFILL-%s" % a.id})
                updated += 1
            else:
                skipped += 1
        return return_Response(
            message="Backfill completed.", status=200,
            data={"updated": updated, "skipped": skipped},
        )

    @http.route(
        BASE + "/<int:aid>", type="http", auth="none",
        methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def candidates_detail(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        return return_Response(
            message="OK", status=200, data=_serialize(rec),
        )

    @http.route(
        BASE + "/<int:aid>", type="http", auth="none",
        methods=["PATCH", "POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidates_update(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        body, err = _read_json_body()
        if err is not None:
            return err

        vals = {}
        for key, (dfield, dtype) in _WRITABLE_FIELDS.items():
            if key not in body:
                continue
            raw = body[key]
            if dtype is bool:
                vals[dfield] = bool(raw)
            elif dtype is float:
                try:
                    vals[dfield] = float(raw or 0)
                except (TypeError, ValueError):
                    continue
            else:
                vals[dfield] = (raw or "").strip() if isinstance(raw, str) else raw

        currentStatus = (body.get("currentStatus") or "").strip().lower()
        if currentStatus:
            if currentStatus in ("on hold", "on_hold", "hold"):
                vals["on_hold"] = True
            elif currentStatus in ("blacklisted", "blocked"):
                vals["is_reapplication_blocked"] = True
            elif currentStatus in ("active", "unhold", "unblock"):
                vals["on_hold"] = False
                vals["is_reapplication_blocked"] = False

        if not vals:
            return return_Response(
                message="No writable fields in request body.",
                status=400,
            )

        rec.write(vals)
        return return_Response(
            message="Candidate updated.", status=200,
            data=_serialize(rec),
        )

    @http.route(
        BASE + "/<int:aid>", type="http", auth="none",
        methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidates_delete(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        rec.write({"active": False})
        return request.make_response("", status=204)

    @http.route(
        BASE + "/<int:aid>/advance-stage", type="http", auth="none",
        methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidates_advance_stage(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        Stage = request.env["hr.recruitment.stage"].sudo()
        current = rec.stage_id
        job_id = rec.job_id.id if rec.job_id else False
        stage_domain = ["|", ("job_ids", "=", False), ("job_ids", "in", [job_id])]
        stages = Stage.search(stage_domain, order="sequence, id")
        if not stages:
            return return_Response(
                message="No stages configured for this job.", status=400,
            )
        next_stage = None
        found_current = current not in stages
        for s in stages:
            if found_current:
                next_stage = s
                break
            if s.id == (current.id if current else False):
                found_current = True
        if not next_stage:
            return return_Response(
                message="Candidate is already at the final stage.",
                status=400,
            )
        rec.write({"stage_id": next_stage.id})
        return return_Response(
            message="Stage advanced to %s." % next_stage.name,
            status=200, data=_serialize(rec),
        )

    @http.route(
        BASE + "/<int:aid>/screen", type="http", auth="none",
        methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidates_screen(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        try:
            rec.action_screen_resume()
        except Exception as exc:
            _logger.exception(
                "candidates_screen failed for hr.applicant %s", aid,
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
        BASE + "/<int:aid>/resume/download", type="http", auth="none",
        methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def candidates_resume_download(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        if not (rec.resume_url or "").strip():
            return return_Response(
                message="This candidate has no resume URL.",
                status=404,
            )
        return request.redirect(rec.resume_url, code=302)
