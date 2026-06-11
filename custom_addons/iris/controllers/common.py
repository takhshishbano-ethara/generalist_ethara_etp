"""Shared helpers for the IRIS REST API controllers.

Pagination, JSON-body parsing, group guards, error mapping, role resolution
and record serializers used by the route modules (``candidates.py`` /
``screenings.py`` / ``interviews.py`` / ``batches.py`` / ``roles.py`` /
``jds.py`` / ``assessments.py``). The response envelope itself comes from
``api_auth_gateway`` so IRIS matches the platform-wide ``/api/v1/...`` API
shape (``return_Response``).
"""

import functools
import json
import logging

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response

_logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200

CANDIDATE_STATES = (
    "draft",
    "screening",
    "shipped",
    "hold",
    "blocked",
    "pending_block",
    "needs_review",
    "interview_ready",
    "interviewed",
    "scored",
    "hired",
    "rejected",
)
SCREENING_VERDICTS = ("ship", "hold", "block")
FINAL_DECISIONS = ("hired", "rejected")
BLOCK_KINDS = ("credibility", "competence")
RESCREEN_REASONS = ("hold_evidence", "batch_consistency")
BATCH_STATES = (
    "draft",
    "screening",
    "consistency",
    "done",
    "failed",
    "cancelled",
)
JD_STATES = (
    "draft",
    "critiquing",
    "critiqued",
    "rewriting",
    "rewritten",
    "approved",
)
ASSESSMENT_STATUSES = ("draft", "sent", "submitted", "reviewed")
ASSESSMENT_RATINGS = (
    "exceptional",
    "above_average",
    "average",
    "below_average",
    "poor",
)
ASSESSMENT_RECOMMENDATIONS = ("hire", "lean_hire", "lean_no_hire", "no_hire")


# ---------------------------------------------------------------------------
# Coercion + pagination
# ---------------------------------------------------------------------------
def coerce_int(value, default):
    """Best-effort int conversion; return ``default`` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_bool(value, default=None):
    """Best-effort bool conversion for JSON / query-string values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def paginate(params):
    """Read ``page`` / ``limit`` from params and clamp to safe bounds.

    Returns ``(page, limit, offset)`` with ``limit`` clamped to
    ``[1, MAX_LIMIT]`` and ``page`` clamped to ``>= 1``.
    """
    page = max(1, coerce_int(params.get("page"), 1))
    limit = min(
        max(1, coerce_int(params.get("limit"), DEFAULT_LIMIT)),
        MAX_LIMIT,
    )
    offset = (page - 1) * limit
    return page, limit, offset


def pagination_block(total, page, limit):
    """Standard pagination metadata block for list responses."""
    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "total_records": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Request body parsing
# ---------------------------------------------------------------------------
def read_json_body():
    """Parse the request body as a JSON object.

    Returns ``(data_dict, None)`` on success (an empty body parses to ``{}``)
    or ``(None, response)`` where ``response`` is a ready-to-return 400.
    """
    raw = request.httprequest.get_data()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, return_Response(
            message="Malformed JSON body.",
            status=400,
            errors=["Malformed JSON body."],
        )
    if not isinstance(data, dict):
        return None, return_Response(
            message="JSON body must be an object.",
            status=400,
            errors=["JSON body must be an object."],
        )
    return data, None


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
def handle_api_errors(func):
    """Map model exceptions to the platform JSON envelope.

    ``UserError`` / ``ValidationError`` → 400 (business rule violations,
    e.g. re-screen without evidence or a missing OpenRouter key),
    ``AccessError`` → 403, anything else → logged + generic 500. The cursor
    is rolled back before returning so partial writes never get committed by
    the normal HTTP dispatch commit.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AccessError as exc:
            request.env.cr.rollback()
            return return_Response(message=str(exc), status=403, errors=[str(exc)])
        except (ValidationError, UserError) as exc:
            request.env.cr.rollback()
            return return_Response(message=str(exc), status=400, errors=[str(exc)])
        except Exception:  # noqa: BLE001 - last-resort API boundary handler
            request.env.cr.rollback()
            _logger.exception("IRIS API: unhandled error in %s", func.__name__)
            return return_Response(
                message="Internal server error.",
                status=500,
                errors=["Internal server error."],
            )

    return wrapper


# ---------------------------------------------------------------------------
# Group guards (validate_token has already bound env.uid to the token user)
# ---------------------------------------------------------------------------
def _require_iris_user():
    """Return a 403 response if the caller has no IRIS access, else None."""
    user = request.env.user
    if user.has_group("iris.group_iris_user") or user.has_group(
        "iris.group_iris_manager",
    ):
        return None
    return return_Response(
        message="You are not allowed to access IRIS data.",
        status=403,
    )


def _require_iris_manager():
    """Return a 403 response if the caller is not an IRIS manager, else None."""
    if request.env.user.has_group("iris.group_iris_manager"):
        return None
    return return_Response(
        message="Only IRIS managers can perform this action.",
        status=403,
    )


# ---------------------------------------------------------------------------
# Role resolution (v1.1 — multi-role infrastructure)
# ---------------------------------------------------------------------------
def _role_400(prefix):
    """400 response listing the valid (active, non-legacy) roles."""
    roles = request.env["iris.role.profile"].sudo().search(
        [("active", "=", True), ("is_legacy", "=", False)],
        order="sequence, id",
    )
    valid = ", ".join(f"{r.code} ({r.name})" for r in roles) or "none configured"
    msg = f"{prefix} Valid roles: {valid}."
    return return_Response(message=msg, status=400, errors=[msg])


def _resolve_role(data, required=False):
    """Resolve a role profile from a request body → ``(role, None)``.

    Accepts (in precedence order, never silently coercing between them):

    * ``role_id``    — integer id,
    * ``role_code``  — the stable slug,
    * ``target_role``— legacy case-insensitive exact name match.

    Domain is always active + non-legacy. Returns ``(None, response)`` with
    a 400 listing the valid roles when the requested role does not resolve,
    and ``(None, None)`` when no role key was provided and ``required`` is
    False (``required=True`` turns that into the same 400).
    """
    Role = request.env["iris.role.profile"].sudo()
    domain = [("active", "=", True), ("is_legacy", "=", False)]

    raw_id = data.get("role_id")
    if raw_id not in (None, "", False):
        role_id = coerce_int(raw_id, None)
        role = (
            Role.search(domain + [("id", "=", role_id)], limit=1)
            if role_id is not None
            else Role
        )
        if not role:
            return None, _role_400(f"Unknown role_id '{raw_id}'.")
        return role, None

    raw_code = (data.get("role_code") or "").strip()
    if raw_code:
        role = Role.search(domain + [("code", "=", raw_code)], limit=1)
        if not role:
            return None, _role_400(f"Unknown role_code '{raw_code}'.")
        return role, None

    raw_name = (data.get("target_role") or "").strip()
    if raw_name:
        role = Role.search(domain + [("name", "=ilike", raw_name)], limit=1)
        if not role:
            return None, _role_400(f"Unknown target_role '{raw_name}'.")
        return role, None

    if required:
        return None, _role_400(
            "Provide a role via role_id, role_code or target_role."
        )
    return None, None


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _iso(value):
    """``date``/``datetime`` → ISO-8601 string, guarded for False/None."""
    return value.isoformat() if value else None


def _maybe_iso(value):
    """ISO-format date-like values, pass strings through, False → None."""
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _m2o(rec):
    """Many2one → ``{"id": N, "name": "..."}`` or None when unset."""
    if not rec:
        return None
    return {"id": rec.id, "name": rec.display_name or ""}


def _llm_meta(rec):
    """LLM audit metadata block shared by all three artifact serializers.

    Only included in ``full=True`` serializations; ``llm_raw_response`` is
    deliberately NOT exposed over the API (heavy + already persisted in DB).
    """
    return {
        "llm_error": rec.llm_error or None,
        "llm_model_used": rec.llm_model_used or None,
        "llm_prompt_tokens": rec.llm_prompt_tokens or 0,
        "llm_completion_tokens": rec.llm_completion_tokens or 0,
        "llm_cost_usd": rec.llm_cost_usd or 0.0,
        "llm_latency_ms": rec.llm_latency_ms or 0,
        "llm_started_at": _iso(rec.llm_started_at),
        "llm_completed_at": _iso(rec.llm_completed_at),
    }


def _role_brief(rec):
    """Role profile → ``{"id", "code", "name"}`` or None when unset."""
    if not rec:
        return None
    return {"id": rec.id, "code": rec.code or None, "name": rec.name or None}


def _candidate_summary(rec):
    """Compact candidate dict for list responses."""
    return {
        "id": rec.id,
        "reference": rec.reference or None,
        "name": rec.name or None,
        "email": rec.email or None,
        "phone": rec.phone or None,
        "target_role": rec.target_role or None,
        "role": _role_brief(rec.role_id),
        "batch": (
            {"id": rec.batch_id.id, "name": rec.batch_id.name or None}
            if rec.batch_id
            else None
        ),
        "state": rec.state,
        "current_verdict": rec.current_verdict or None,
        "final_recommendation": rec.final_recommendation or None,
        "hold_deadline": _iso(rec.hold_deadline),
        "has_resume": bool(rec.resume_filename or rec.resume_s3_key),
        "create_date": _iso(rec.create_date),
    }


def _candidate_detail(rec):
    """Full candidate dict, including nested artifact summaries."""
    data = _candidate_summary(rec)
    data.update(
        {
            "tech_date_reference": _maybe_iso(rec.tech_date_reference),
            "jd_id": rec.jd_id.id if rec.jd_id else None,
            "duplicate_warning": rec.duplicate_warning or None,
            "duplicates": [
                {
                    "id": dup.id,
                    "reference": dup.reference or None,
                    "name": dup.name or None,
                }
                for dup in rec.duplicate_candidate_ids
            ],
            "resume_filename": rec.resume_filename or None,
            "resume_s3_key": rec.resume_s3_key or None,
            "resume_uploaded_at": _iso(rec.resume_uploaded_at),
            "current_screening_id": (
                rec.current_screening_id.id if rec.current_screening_id else None
            ),
            "screenings": [_screening_dict(s) for s in rec.screening_ids],
            "interviews": [_interview_dict(i) for i in rec.interview_ids],
            "scorecards": [
                _scorecard_dict(sc)
                for sc in rec.interview_ids.mapped("scorecard_ids")
            ],
        },
    )
    return data


def _screening_dict(rec, full=False):
    """Serialize an ``iris.screening``; markdown + evidence only when full."""
    data = {
        "id": rec.id,
        "candidate_id": rec.candidate_id.id,
        "candidate_name": rec.candidate_id.name or None,
        "attempt": rec.attempt or 0,
        "is_rescreen": bool(rec.is_rescreen),
        "parent_screening_id": (
            rec.parent_screening_id.id if rec.parent_screening_id else None
        ),
        "verdict": rec.verdict or None,
        "verdict_manual": bool(rec.verdict_manual),
        "llm_status": rec.llm_status,
        "screened_at": _iso(rec.screened_at),
        "hold_deadline": _iso(rec.hold_deadline),
        "auto_blocked": bool(rec.auto_blocked),
        "create_date": _iso(rec.create_date),
    }
    if full:
        data.update(
            {
                "markdown_record": rec.markdown_record or None,
                "verification_evidence": rec.verification_evidence or None,
                "evidence_recorded_by": _m2o(rec.evidence_recorded_by),
                "evidence_recorded_at": _iso(rec.evidence_recorded_at),
                "requested_by": _m2o(rec.requested_by_id),
                "block_signoff_state": rec.block_signoff_state,
                "block_kind": rec.block_kind or None,
                "block_proposed_by": _m2o(rec.block_proposed_by_id),
                "block_proposed_at": _iso(rec.block_proposed_at),
                "block_signed_off_by": _m2o(rec.block_signed_off_by_id),
                "block_signed_off_at": _iso(rec.block_signed_off_at),
                "block_signoff_rejection_reason": (
                    rec.block_signoff_rejection_reason or None
                ),
                "rescreen_reason": rec.rescreen_reason or None,
                "batch_id": rec.batch_id.id if rec.batch_id else None,
                "clarifying_questions": (
                    rec.clarifying_questions_markdown or None
                ),
                **_llm_meta(rec),
            },
        )
    return data


def _interview_dict(rec, full=False):
    """Serialize an ``iris.interview``; guide + notes only when full."""
    data = {
        "id": rec.id,
        "candidate_id": rec.candidate_id.id,
        "candidate_name": rec.candidate_id.name or None,
        "screening_id": rec.screening_id.id if rec.screening_id else None,
        "llm_status": rec.llm_status,
        "notes_submitted": bool(rec.notes_submitted),
        "interviewer_id": rec.interviewer_id.id if rec.interviewer_id else None,
        "interviewer_name": (
            rec.interviewer_name
            or (rec.interviewer_id.name if rec.interviewer_id else None)
            or None
        ),
        "interviewed_at": _iso(rec.interviewed_at),
        "create_date": _iso(rec.create_date),
    }
    if full:
        data.update(
            {
                "guide_markdown": rec.guide_markdown or None,
                "notes": rec.notes or None,
                **_llm_meta(rec),
            },
        )
    return data


def _scorecard_dict(rec, full=False):
    """Serialize an ``iris.scorecard``; markdown + snapshot only when full."""
    data = {
        "id": rec.id,
        "interview_id": rec.interview_id.id,
        "candidate_id": rec.candidate_id.id if rec.candidate_id else None,
        "candidate_name": rec.candidate_id.name or None,
        "recommendation": rec.recommendation or None,
        "recommendation_manual": bool(rec.recommendation_manual),
        "llm_status": rec.llm_status,
        "create_date": _iso(rec.create_date),
    }
    if full:
        data.update(
            {
                "scorecard_markdown": rec.scorecard_markdown or None,
                "notes_snapshot": rec.notes_snapshot or None,
                **_llm_meta(rec),
            },
        )
    return data


def _role_dict(rec, full=False):
    """Serialize an ``iris.role.profile``.

    Per-role prompt overrides are deliberately NOT exposed over the API.
    """
    data = {
        "id": rec.id,
        "code": rec.code or None,
        "name": rec.name or None,
        "sequence": rec.sequence,
        "active": bool(rec.active),
        "is_legacy": bool(rec.is_legacy),
    }
    if full:
        data.update(
            {
                "description": rec.description or None,
                "competence_guidance": rec.competence_guidance or None,
                "default_tech_date_reference": (
                    rec.default_tech_date_reference or None
                ),
                "candidate_count": rec.candidate_count,
            },
        )
    return data


def _batch_member_dict(candidate):
    """Member entry for batch detail: candidate summary + latest screening."""
    data = _candidate_summary(candidate)
    screening = candidate.screening_ids.sorted("id", reverse=True)[:1]
    data["screening"] = (
        {
            "id": screening.id,
            "verdict": screening.verdict or None,
            "llm_status": screening.llm_status,
            "block_signoff_state": screening.block_signoff_state,
        }
        if screening
        else None
    )
    return data


def _batch_summary(rec):
    """Compact ``iris.screening.batch`` dict for list responses."""
    return {
        "id": rec.id,
        "name": rec.name or None,
        "state": rec.state,
        "role": _role_brief(rec.role_id),
        "member_count": rec.member_count,
        "settled_count": rec.settled_count,
        "auto_run_consistency": bool(rec.auto_run_consistency),
        "consistency_llm_status": rec.llm_status,
        "inconsistency_count": rec.inconsistency_count or 0,
        "revision_advisory_count": rec.revision_advisory_count or 0,
        "total_cost_usd": rec.total_cost_usd or 0.0,
        "owner": _m2o(rec.user_id),
        "create_date": _iso(rec.create_date),
    }


def _batch_detail(rec):
    """Full batch dict: members + per-member screening status + costs."""
    data = _batch_summary(rec)
    data.update(
        {
            "notes": rec.notes or None,
            "members": [_batch_member_dict(c) for c in rec.candidate_ids],
            "blocking_candidates": [
                {
                    "id": c.id,
                    "reference": c.reference or None,
                    "name": c.name or None,
                    "state": c.state,
                }
                for c in rec.blocking_candidate_ids
            ],
            "report_available": bool(
                rec.state == "done" and rec.batch_report_markdown
            ),
            "consistency": _llm_meta(rec),
        },
    )
    return data


def _jd_artifact_dict(rec, full=False):
    """Serialize an ``iris.jd.artifact``; markdown + LLM meta when full."""
    data = {
        "id": rec.id,
        "jd_id": rec.jd_id.id,
        "operation": rec.operation,
        "attempt": rec.attempt or 0,
        "llm_status": rec.llm_status,
        "create_date": _iso(rec.create_date),
    }
    if full:
        data.update(
            {
                "markdown_result": rec.markdown_result or None,
                **_llm_meta(rec),
            },
        )
    return data


def _jd_summary(rec):
    """Compact ``iris.job.description`` dict for list responses."""
    return {
        "id": rec.id,
        "name": rec.name or None,
        "company_name": rec.company_name or None,
        "state": rec.state,
        "has_fillins": bool(rec.has_fillins),
        "artifact_count": rec.artifact_count,
        "approved_by": _m2o(rec.approved_by),
        "approved_at": _iso(rec.approved_at),
        "create_date": _iso(rec.create_date),
    }


def _jd_detail(rec):
    """Full JD dict: texts + current artifacts + artifact summaries."""
    data = _jd_summary(rec)
    data.update(
        {
            "raw_jd": rec.raw_jd or None,
            "final_jd": rec.final_jd or None,
            "source_filename": rec.source_filename or None,
            "current_critique_id": (
                rec.current_critique_id.id if rec.current_critique_id else None
            ),
            "current_rewrite_id": (
                rec.current_rewrite_id.id if rec.current_rewrite_id else None
            ),
            "artifacts": [_jd_artifact_dict(a) for a in rec.artifact_ids],
        },
    )
    return data


def _assessment_dict(rec, full=False):
    """Serialize an ``iris.assessment``; texts + LLM meta when full."""
    data = {
        "id": rec.id,
        "candidate_id": rec.candidate_id.id,
        "candidate_name": rec.candidate_id.name or None,
        "status": rec.status,
        "rating": rec.rating or None,
        "recommendation": rec.recommendation or None,
        "llm_status": rec.llm_status,
        "sent_at": _iso(rec.sent_at),
        "submitted_at": _iso(rec.submitted_at),
        "submission_filename": rec.submission_filename or None,
        "reviewed_by": _m2o(rec.reviewed_by),
        "reviewed_at": _iso(rec.reviewed_at),
        "create_date": _iso(rec.create_date),
    }
    if full:
        data.update(
            {
                "brief": rec.brief or None,
                "submission_text": rec.submission_text or None,
                "llm_draft_markdown": rec.llm_draft_markdown or None,
                "summary": rec.summary or None,
                "strengths": rec.strengths or None,
                "concerns": rec.concerns or None,
                "fit_for_current_need": rec.fit_for_current_need or None,
                "recommendation_conditions": (
                    rec.recommendation_conditions or None
                ),
                **_llm_meta(rec),
            },
        )
    return data
