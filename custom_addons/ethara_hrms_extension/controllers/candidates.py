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
from odoo.addons.ethara_hrms_extension.constants import (
    PIPELINE_STATUS_KEYS,
    PIPELINE_STATUS_LABELS,
    STATUS_TO_PIPELINE_BUCKET,
    REJECTED_HIRING_STATUSES,
    _IN_ASSESSMENT_STATUSES,
    RESUME_RECOMMENDATION_OUT,
)

_logger = logging.getLogger(__name__)
BASE = "/api/v1/candidates"

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


def _current_stage(applicant):
    if applicant.pipeline_status == "rejected" or applicant.refuse_reason_id:
        return "rejected"
    effective = _derive_effective_status(applicant)
    if effective in REJECTED_HIRING_STATUSES:
        return "rejected"
    return STATUS_TO_PIPELINE_BUCKET.get(effective) or applicant.pipeline_status or "applied"


def _current_status(applicant):
    if applicant.is_reapplication_blocked:
        return "Blacklisted"
    if applicant.on_hold:
        return "On Hold"
    return PIPELINE_STATUS_LABELS.get(_current_stage(applicant), "Applied")


def _derive_effective_status(applicant):
    raw = applicant.status or "pending"
    if raw != "pending":
        return raw
    if applicant.pipeline_status == "rejected" or applicant.resume_recommendation == "reject":
        return "resume_screening_rejected"
    if applicant.resume_recommendation == "shortlist":
        state = _f(applicant, "latest_assessment_state")
        result = _f(applicant, "latest_assessment_result")
        if state == "scored":
            if result == "pass":
                return "assessment_passed"
            if result == "fail":
                return "assessment_rejected"
            return "assessment_pending_review"
        if state == "submitted":
            return "assessment_pending_review"
        if state == "in_progress":
            return "assessment_in_progress"
        if state == "sent":
            return "pending_assessment"
        return "resume_screening_passed"
    if applicant.job_id:
        return "resume_screening_in_progress"
    return "pending"


def _active_application_conflict(candidate_user_id, exclude_applicant_id):
    if not candidate_user_id:
        return request.env["hr.applicant"].sudo().browse()
    domain = [
        ("candidate_user_id", "=", candidate_user_id),
        ("active", "=", True),
        ("refuse_reason_id", "=", False),
        ("pipeline_status", "!=", "rejected"),
    ]
    if exclude_applicant_id:
        domain.append(("id", "!=", exclude_applicant_id))
    return request.env["hr.applicant"].sudo().search(domain, limit=1)


def _conflict_summary(conflict):
    if not conflict:
        return None
    return {
        "applicantId":    conflict.id,
        "jobId":          conflict.job_id.id if conflict.job_id else None,
        "jobTitle":       conflict.job_id.name if conflict.job_id else None,
        "pipelineStatus": conflict.pipeline_status or "applied",
        "currentStatus":  PIPELINE_STATUS_LABELS.get(
            conflict.pipeline_status or "applied", "Applied",
        ),
    }


def _f(rec, fname):
    return rec[fname] if rec and fname in rec._fields else None


def _selection_label(rec, field_name):
    if not rec or field_name not in rec._fields:
        return None
    raw = rec[field_name]
    if not raw:
        return None
    selection = dict(rec._fields[field_name].selection or [])
    return selection.get(raw) or raw


def _int_or_none(value):
    try:
        return int(value) if value not in (None, False, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return None
    return f or None


def _str_or_none(value):
    if value in (None, False, ""):
        return None
    return value


def _serialize_position(job):
    if not job:
        return None
    responsibilities = []
    if "responsibility_ids" in job._fields:
        responsibilities = [
            r.name for r in job.responsibility_ids.sorted("sequence") if r.name
        ]
    benefits = []
    if "benefit_ids" in job._fields:
        benefits = [
            b.name for b in job.benefit_ids.sorted("sequence") if b.name
        ]
    preferred_skills = []
    if "preferred_skill_ids" in job._fields:
        preferred_skills = [s.name for s in job.preferred_skill_ids if s.name]
    return {
        "id":              str(job.id),
        "title":           job.name,
        "slug":            _str_or_none(_f(job, "slug")),
        "department":      job.department_id.name if job.department_id else None,
        "summary":         _str_or_none(_f(job, "summary")),
        "description":     _str_or_none(job.description),
        "location":        _str_or_none(_f(job, "job_location")) or (
                              job.address_id.city if job.address_id else None
                           ),
        "employmentType":  _selection_label(job, "employment_type"),
        "workMode":        _selection_label(job, "work_mode"),
        "experienceLevel": _selection_label(job, "experience_level"),
        "experienceYears": _float_or_none(_f(job, "experience_years")),
        "salaryBracket":   _str_or_none(_f(job, "salary_bracket")),
        "budgetCtc":       _float_or_none(_f(job, "budget_ctc")),
        "responsibilities": responsibilities,
        "requirements":    [],
        "preferredSkills": preferred_skills,
        "benefits":        benefits,
        "featured":        bool(_f(job, "is_featured")),
        "openings":        job.no_of_recruitment or 1,
        "postedAt":        _iso(_f(job, "posted_at") or job.create_date),
        "urgencyLevel":    _int_or_none(_f(job, "urgency_level")),
        "isActive":        bool(job.active),
        "screeningPrompt": _str_or_none(_f(job, "screening_prompt")),
        "candidateCount":  _int_or_none(_f(job, "application_count")),
        "approvalStatus":  _f(job, "approval_status") or None,
        "approvalRequestedAt": _iso(_f(job, "approval_requested_at")),
        "approvalDecidedAt": _iso(_f(job, "approval_decided_at")),
        "approvalEmailSentAt": _iso(_f(job, "approval_email_sent_at")),
        "approvalRecipientEmail": _str_or_none(_f(job, "approval_recipient_email")),
        "requestedBy": (
            job.requested_by_id.name if _f(job, "requested_by_id")
            else _str_or_none(_f(job, "external_requested_by"))
        ),
        "approvedBy": job.approved_by_id.name if _f(job, "approved_by_id") else None,
        "reviewedByEmail": (
            job.approved_by_id.login if _f(job, "approved_by_id")
            else (job.rejected_by_id.login if _f(job, "rejected_by_id") else None)
        ),
        "rejectionReason": _str_or_none(_f(job, "rejection_reason")),
        "createdAt":       _iso(job.create_date),
        "updatedAt":       _iso(job.write_date),
    }


def _serialize_college(college):
    if not college:
        return None
    return {
        "id":         college.id,
        "name":       college.name,
        "shortName":  _f(college, "short_name") or _f(college, "code"),
        "isActive":   bool(college.active) if "active" in college._fields else True,
        "createdAt":  _iso(college.create_date),
        "updatedAt":  _iso(college.write_date),
    }


def _serialize_stage_logs(applicant):
    logs = []
    hist = applicant.job_history_ids if "job_history_ids" in applicant._fields else []
    for h in hist.sorted(lambda r: r.create_date or _iso(r.create_date), reverse=True):
        logs.append({
            "id": h.id,
            "candidateId": applicant.id,
            "fromStage": h.previous_job_id.name if h.previous_job_id else None,
            "toStage":   h.new_job_id.name if h.new_job_id else None,
            "changedBy": h.changed_by_id.id if h.changed_by_id else None,
            "changedByName": h.changed_by_id.name if h.changed_by_id else None,
            "notes":     _f(h, "notes"),
            "createdAt": _iso(h.create_date),
        })
    return logs


def _serialize_documents(applicant):
    docs = []
    Attachment = applicant.env["ir.attachment"].sudo()
    atts = Attachment.search([
        ("res_model", "=", "hr.applicant"),
        ("res_id", "=", applicant.id),
    ], order="create_date desc")
    for a in atts:
        docs.append({
            "id":         a.id,
            "candidateId": applicant.id,
            "type":       a.name.split(".")[0].lower() if a.name else "attachment",
            "fileName":   a.name,
            "fileUrl":    "/web/content/%d" % a.id,
            "fileSize":   a.file_size,
            "mimeType":   a.mimetype,
            "status":     "uploaded",
            "createdAt":  _iso(a.create_date),
            "updatedAt":  _iso(a.write_date),
        })
    return docs


def _serialize_contract(applicant):
    Contract = applicant.env.get("documenso.contract")
    if Contract is None:
        return None
    c = applicant.env["documenso.contract"].sudo().search([
        ("applicant_id" if "applicant_id" in applicant.env["documenso.contract"]._fields
         else "employee_id", "=", applicant.id),
    ], limit=1)
    if not c:
        return None
    return {
        "id":           c.id,
        "candidateId":  applicant.id,
        "status":       _f(c, "status") or "draft",
        "documensoId":  _f(c, "documenso_id"),
        "templateId":   _f(c, "template_id"),
        "signedUrl":    _f(c, "signed_url"),
        "pdfUrl":       _f(c, "pdf_url"),
        "sentAt":       _iso(_f(c, "sent_at")),
        "signedAt":     _iso(_f(c, "signed_at")),
        "expiresAt":    _iso(_f(c, "expires_at")),
        "ctc":          _f(c, "ctc"),
        "joiningDate":  _iso(_f(c, "joining_date")),
        "createdAt":    _iso(c.create_date),
        "updatedAt":    _iso(c.write_date),
    }


def _override_is_fresh(applicant):
    if not applicant.resume_manual_override_at:
        return False
    if not applicant.resume_screened_at:
        return True
    return applicant.resume_manual_override_at >= applicant.resume_screened_at


def _serialize(applicant):
    payload = {}
    try:
        payload = json.loads(applicant.resume_screening_payload or "{}")
    except (ValueError, TypeError):
        payload = {}

    override = None
    if applicant.resume_manual_override_reason and _override_is_fresh(applicant):
        override = {
            "reason": applicant.resume_manual_override_reason,
            "at": _iso(applicant.resume_manual_override_at),
            "by": (
                applicant.resume_manual_override_by_id.name
                if applicant.resume_manual_override_by_id else None
            ),
        }

    aadhaar = (applicant.aadhaar_number or "").strip() if "aadhaar_number" in applicant._fields else ""
    employee = applicant.employee_id if applicant.employee_id else None
    college = applicant.college_id if "college_id" in applicant._fields and applicant.college_id else None

    return {
        "id":                       applicant.id,
        "candidateId":              applicant.id,
        "accessLevel":              "full",
        "canOpenDetail":            True,
        "candidateCode":            applicant.candidate_code or str(applicant.id),
        "employeeCode":             _f(employee, "employee_code") if employee else None,
        "fullName":                 applicant.partner_name
                                    or (applicant.partner_id.name if applicant.partner_id else "")
                                    or "",
        "personalEmail":            applicant.email_from or "",
        "etharaEmail":              _f(employee, "work_email") if employee else None,
        "phone":                    applicant.partner_phone or "",
        "aadhaarLast4":             aadhaar[-4:] if len(aadhaar) >= 4 else None,
        "gender":                   _f(applicant, "gender"),
        "dateOfBirth":              _iso(_f(applicant, "birthday")),
        "maritalStatus":            _f(applicant, "marital"),
        "sourceType":               applicant.source_type or "direct_application",
        "sourceId":                 applicant.source_reference_id or None,
        "positionId":               applicant.job_id.id if applicant.job_id else None,
        "jobId":                    applicant.job_id.id if applicant.job_id else None,
        "positionTitle":            applicant.job_id.name if applicant.job_id else None,
        "collegeId":                college.id if college else None,
        "vendorId":                 None,
        "stageId":                  applicant.stage_id.id if applicant.stage_id else None,
        "stageName":                applicant.stage_id.name if applicant.stage_id else None,
        "currentStage":             _current_stage(applicant),
        "currentStatus":            _current_status(applicant),
        "status":                   _derive_effective_status(applicant) or None,
        "priorityScore":            applicant.priority_score or 0,
        "onHold":                   bool(applicant.on_hold),
        "isDuplicate":              bool(applicant.is_duplicate),
        "duplicateReason":          applicant.duplicate_reason or None,
        "isReapplicationBlocked":   bool(applicant.is_reapplication_blocked),
        "blacklistReason":          applicant.blacklist_reason or "",
        "activeApplicationConflict": _conflict_summary(
            _active_application_conflict(
                applicant.candidate_user_id.id if applicant.candidate_user_id else None,
                applicant.id,
            )
        ),
        "lastAppliedAt":            _iso(applicant.create_date),
        "resumeUrl":                applicant.resume_url or None,
        "resumeScore":              (applicant.resume_score or 0) if applicant.resume_screened_at else 0,
        "screeningScore":           (applicant.resume_score or 0) if applicant.resume_screened_at else 0,
        "matchScore":               (applicant.resume_score or 0) if applicant.resume_screened_at else 0,
        "resumeSummary":            (applicant.resume_summary or "") if applicant.resume_screened_at else "",
        "resumeText":               applicant.resume_text or None,
        "resumeKeyPoints":          None,
        "recommendation":           RESUME_RECOMMENDATION_OUT.get(applicant.resume_recommendation, "pending"),
        "screeningPayload":         payload if applicant.resume_screened_at else {},
        "manualOverride":           override,
        "llmStatus":                applicant.resume_llm_status,
        "llmModel":                 applicant.resume_llm_model_used,
        "llmError":                 applicant.resume_llm_error,
        "llmPromptTokens":          applicant.resume_llm_prompt_tokens or 0,
        "llmCompletionTokens":      applicant.resume_llm_completion_tokens or 0,
        "llmLatencyMs":             applicant.resume_llm_latency_ms or 0,
        "lastScreenedAt":           _iso(applicant.resume_screened_at),
        "aadhaarExtracted":         None,
        "aadhaarCardUrl":           _f(applicant, "aadhaar_card_url"),
        "portfolioUrl":             _f(applicant, "portfolio_url"),
        "githubUrl":                _f(applicant, "github_url"),
        "linkedinProfile":          _f(applicant, "linkedin_profile"),
        "experienceType":           _f(applicant, "experience"),
        "experienceYears":          _float_or_none(_f(applicant, "experience_years")),
        "currentCompany":           applicant.current_company or None,
        "currentCTC":               _float_or_none(applicant.current_ctc),
        "expectedCTC":              _float_or_none(applicant.expected_ctc),
        "ctcBudgetExceeded":        bool(applicant.ctc_budget_exceeded),
        "noticePeriod":             _int_or_none(applicant.notice_period_days),
        "position":                 _serialize_position(applicant.job_id),
        "college":                  _serialize_college(college),
        "vendor":                   None,
        "contract":                 _serialize_contract(applicant),
        "stageLogs":                _serialize_stage_logs(applicant),
        "evaluations":              [],
        "documents":                _serialize_documents(applicant),
        "complianceForms":          [],
        "escalations":              [],
        "notifications":            [],
        "itRequest":                None,
        "selectionForm":            None,
        "auditLogs":                [],
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
            "expectedCTC", "ctcBudgetExceeded",
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
                RESUME_RECOMMENDATION_OUT.get(r.resume_recommendation, "pending"),
                r.expected_ctc or 0,
                bool(r.ctc_budget_exceeded),
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

    @http.route(
        BASE + "/<int:aid>/progress", type="http", auth="none",
        methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def candidates_progress(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        return return_Response(
            message="OK", status=200, data=_build_progress_payload(rec),
        )

    @http.route(
        BASE + "/<int:aid>/history", type="http", auth="none",
        methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def candidates_history(self, aid, **kwargs):
        rec, err = _applicant_or_404(aid)
        if err is not None:
            return err
        if not rec.candidate_user_id:
            return return_Response(
                message="OK", status=200, data={"applications": []},
            )
        sibs = request.env["hr.applicant"].sudo().with_context(
            active_test=False,
        ).search(
            [
                ("candidate_user_id", "=", rec.candidate_user_id.id),
                ("id", "!=", rec.id),
            ],
            order="create_date desc",
        )
        items = [
            {
                "applicantId":    s.id,
                "jobId":          s.job_id.id if s.job_id else None,
                "jobTitle":       s.job_id.name if s.job_id else None,
                "pipelineStatus": s.pipeline_status or "applied",
                "currentStatus":  _current_status(s),
                "createdAt":      _iso(s.create_date),
                "screenedAt":     _iso(s.resume_screened_at),
                "isRejected":     s.pipeline_status == "rejected"
                                  or bool(s.refuse_reason_id),
                "isArchived":     not s.active,
            }
            for s in sibs
        ]
        return return_Response(
            message="OK", status=200, data={"applications": items},
        )


def _build_progress_payload(rec):
    is_archived = not rec.active

    rec_out = (
        RESUME_RECOMMENDATION_OUT.get(rec.resume_recommendation)
        if rec.resume_recommendation
        else None
    )

    effective_status = _derive_effective_status(rec)
    is_rejected = (
        rec.pipeline_status == "rejected"
        or bool(rec.refuse_reason_id)
        or effective_status in REJECTED_HIRING_STATUSES
    )

    current_bucket = STATUS_TO_PIPELINE_BUCKET.get(effective_status, "applied")
    if current_bucket not in PIPELINE_STATUS_KEYS:
        current_bucket = "applied"
    current_idx = PIPELINE_STATUS_KEYS.index(current_bucket)

    status_val = effective_status
    assessment_in_progress = status_val in _IN_ASSESSMENT_STATUSES
    shortlisted_detail = None
    if rec.resume_screened_at:
        if effective_status == "resume_screening_rejected":
            shortlisted_detail = "Rejected · Score %s" % int(rec.resume_score or 0)
        elif rec_out:
            shortlisted_detail = "Score %s · %s" % (
                int(rec.resume_score or 0),
                rec_out.replace("_", " ").title(),
            )

    assessment_detail = None
    if effective_status in ("assessment_passed", "assessment_rejected"):
        assessment_detail = status_val.replace("_", " ").title()
    elif assessment_in_progress:
        assessment_detail = "In progress"

    fallback_at = _iso(
        _f(rec, "status_updated_at") or rec.write_date or rec.create_date
    )
    step_meta = {
        "applied":     (_iso(rec.create_date), "Registered as candidate"),
        "shortlisted": (_iso(rec.resume_screened_at) or fallback_at, shortlisted_detail),
        "assessment":  (fallback_at if current_idx >= 2 else None, assessment_detail),
    }

    total = len(PIPELINE_STATUS_KEYS)
    steps = []
    for i, key in enumerate(PIPELINE_STATUS_KEYS):
        ev_at, ev_detail = step_meta.get(key, (None, None))
        if i < current_idx:
            step_status = "completed"
            if not ev_at:
                ev_at = fallback_at
        elif i == current_idx:
            step_status = "rejected" if is_rejected else "current"
        else:
            step_status = "pending"
            ev_at = None
            ev_detail = None
        steps.append({
            "key": key,
            "label": PIPELINE_STATUS_LABELS[key],
            "status": step_status,
            "at": ev_at,
            "detail": ev_detail,
        })

    override_block = None
    if rec.resume_manual_override_reason and _override_is_fresh(rec):
        override_block = {
            "reason": rec.resume_manual_override_reason,
            "at":     _iso(rec.resume_manual_override_at),
            "by":     rec.resume_manual_override_by_id.name if rec.resume_manual_override_by_id else None,
        }

    screening_block = {
        "llmStatus":       rec.resume_llm_status or "pending",
        "score":           float(rec.resume_score or 0),
        "recommendation":  rec_out,
        "screenedAt":      _iso(rec.resume_screened_at),
        "model":           rec.resume_llm_model_used or None,
        "override":        override_block,
    }

    progress_percent = int(round((current_idx / max(total - 1, 1)) * 100))

    return {
        "candidateId":     rec.id,
        "candidateName":   rec.partner_name or (rec.partner_id.name if rec.partner_id else None),
        "currentStatus":   _current_status(rec),
        "currentBucket":   "rejected" if is_rejected else PIPELINE_STATUS_KEYS[current_idx],
        "currentIndex":    current_idx,
        "totalSteps":      total,
        "progressPercent": progress_percent,
        "steps":           steps,
        "screening":       screening_block,
        "isRejected":      is_rejected,
        "isArchived":      is_archived,
    }
