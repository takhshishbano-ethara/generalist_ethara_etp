import json
import logging
import re
from datetime import datetime

from dateutil.relativedelta import relativedelta

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .candidates import (
    _iso,
    _serialize,
    _serialize_position,
    _serialize_college,
    _serialize_contract,
    _build_progress_payload,
)

_logger = logging.getLogger(__name__)

BASE = "/api/v1/candidates/me"

_PROFILE_WRITABLE = {
    "phone":           ("partner_phone",      "phone"),
    "email":           ("email_from",         "email"),
    "gender":          ("gender",             "gender"),
    "dateOfBirth":     ("birthday",           "date_past"),
    "maritalStatus":   ("marital",            "str"),
    "experienceType":  ("experience",         "experience_type"),
    "experienceYears": ("experience_years",   "float_nonneg"),
    "currentCompany":  ("current_company",    "company"),
    "currentRole":     ("current_role",       "str"),
    "currentCTC":      ("current_ctc",        "ctc_lpa"),
    "expectedCTC":     ("expected_ctc",       "ctc_lpa"),
    "noticePeriod":    ("notice_period_days", "int_nonneg"),
    "collegeId":       ("college_id",         "many2one"),
}

# fullName is required in the request body but never written — the display name
# is fixed at Aadhaar registration and this endpoint only validates it.
_PROFILE_REQUIRED = ("fullName", "phone", "gender", "dateOfBirth", "experienceType")

_PARTNER_STR_FIELDS = {
    "bio":      "bio_data",
    "location": "location",
}

_PORTAL_APP_KEYS = frozenset({
    "id", "candidateCode", "fullName", "personalEmail", "phone",
    "sourceType", "currentStage", "currentStatus", "status",
    "isDuplicate", "isReapplicationBlocked",
    "aadhaarLast4", "gender", "dateOfBirth", "maritalStatus",
    "experienceType", "experienceYears", "currentCompany",
    "currentCTC", "expectedCTC", "noticePeriod",
    "positionId", "lastAppliedAt", "createdAt", "position",
})
_PORTAL_POSITION_KEYS = frozenset({
    "id", "title", "summary", "department", "location", "workMode",
    "experienceLevel", "requirements", "experienceYears",
})


def _slim_for_portal(payload):
    if not payload:
        return payload
    trimmed = {k: v for k, v in payload.items() if k in _PORTAL_APP_KEYS}
    pos = trimmed.get("position")
    if isinstance(pos, dict):
        trimmed["position"] = {k: v for k, v in pos.items() if k in _PORTAL_POSITION_KEYS}
    return trimmed

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_NON_DIGIT_RE = re.compile(r"\D")
_COMPANY_ALLOWED_RE = re.compile(r"^[\w\s.,&'\-()]+$", re.UNICODE)
_GENDER_ALLOWED = frozenset({"male", "female", "other"})
_EXPERIENCE_ALLOWED = frozenset({"fresher", "experienced"})


def _read_json_body():
    try:
        raw = request.httprequest.get_data(cache=False, as_text=True) or ""
        return (json.loads(raw) if raw.strip() else {}), None
    except (ValueError, TypeError) as exc:
        return None, return_Response(
            message="Request body must be valid JSON.",
            status=400,
            errors=[str(exc)],
        )


def _coerce_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _applications_for_user():
    user = request.env.user
    if not user or not user.exists():
        return request.env["hr.applicant"].sudo().browse()
    return request.env["hr.applicant"].sudo().with_context(
        active_test=False,
    ).search(
        [("candidate_user_id", "=", user.id)],
        order="write_date desc, create_date desc, id desc",
    )


def _current_application(applications):
    if not applications:
        return None
    for app in applications:
        if (
            app.active
            and not app.refuse_reason_id
            and app.pipeline_status != "rejected"
        ):
            return app
    return applications[0]


REAPPLY_COOLDOWN_MONTHS = 3


def _reapply_blocked_until(user_id, job_id):
    if not user_id or not job_id:
        return None
    cutoff = fields.Datetime.now() - relativedelta(months=REAPPLY_COOLDOWN_MONTHS)
    prior = request.env["hr.applicant"].sudo().with_context(
        active_test=False,
    ).search(
        [
            ("candidate_user_id", "=", user_id),
            ("job_id", "=", job_id),
            ("write_date", ">=", cutoff),
            "|",
            ("pipeline_status", "=", "rejected"),
            ("refuse_reason_id", "!=", False),
        ],
        order="write_date desc",
        limit=1,
    )
    if not prior:
        return None
    return prior.write_date + relativedelta(months=REAPPLY_COOLDOWN_MONTHS)


def _email_verified_at(user):
    return user.create_date if user and user.exists() else None


def _validate_and_coerce(raw, kind):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    if kind == "str":
        return str(raw).strip(), None
    if kind == "phone":
        digits = _NON_DIGIT_RE.sub("", str(raw))
        if len(digits) != 10:
            return None, "Enter a valid 10-digit phone number."
        return digits, None
    if kind == "email":
        text = str(raw).strip()
        if not _EMAIL_RE.match(text):
            return None, "Enter a valid email address."
        return text, None
    if kind == "gender":
        text = str(raw).strip().lower()
        if text not in _GENDER_ALLOWED:
            return None, "Select a valid gender (male, female, or other)."
        return text, None
    if kind == "date_past":
        parsed = _coerce_date(raw)
        if not parsed:
            return None, "Enter a valid date of birth (YYYY-MM-DD)."
        if parsed >= datetime.utcnow().date():
            return None, "Date of birth must be in the past."
        return parsed, None
    if kind == "experience_type":
        text = str(raw).strip().lower()
        if text not in _EXPERIENCE_ALLOWED:
            return None, "Select experience type (fresher or experienced)."
        return text, None
    if kind == "float_nonneg":
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, "Must be a number."
        if val < 0:
            return None, "Cannot be negative."
        return val, None
    if kind == "ctc_lpa":
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, "Must be a number."
        if val < 0:
            return None, "Cannot be negative."
        if val > 100:
            return None, "Cannot exceed 100 LPA."
        return val, None
    if kind == "company":
        text = str(raw).strip()
        if len(text) > 100:
            return None, "Cannot exceed 100 characters."
        if not _COMPANY_ALLOWED_RE.match(text):
            return None, "Only letters, numbers, spaces, and . , & - ' ( ) are allowed."
        return text, None
    if kind == "int_nonneg":
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None, "Must be a whole number."
        if val < 0:
            return None, "Cannot be negative."
        if val > 365:
            return None, "Cannot exceed 365 days."
        return val, None
    if kind == "many2one":
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, "Invalid reference id."
    return raw, None


def _overview_payload():
    user = request.env.user
    applications = _applications_for_user()
    current = _current_application(applications)
    current_id = current.id if current else None
    others = [a for a in applications if a.id != current_id]
    return {
        "currentApplication": _slim_for_portal(_serialize(current)) if current else None,
        "applications": [_slim_for_portal(_serialize(a)) for a in others],
        "emailVerified": True,
        "emailVerifiedAt": _iso(_email_verified_at(user)),
        "progress": _build_progress_payload(current) if current else None,
    }


class EtharaCandidateMeApi(http.Controller):

    @http.route(
        BASE, type="http", auth="none", methods=["GET"],
        csrf=False, cors="*",
    )
    @validate_token
    def candidate_me(self, **kwargs):
        return return_Response(
            message="OK", status=200, data=_overview_payload(),
        )

    @http.route(
        BASE + "/apply", type="http", auth="none", methods=["POST"],
        csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidate_me_apply(self, **kwargs):
        body, err = _read_json_body()
        if err is not None:
            return err

        position_id_raw = body.get("positionId") or body.get("job_id")
        try:
            position_id = int(position_id_raw)
        except (TypeError, ValueError):
            return return_Response(
                message="`positionId` is required and must be an integer.",
                status=400,
            )

        job = request.env["hr.job"].sudo().browse(position_id).exists()
        if not job:
            return return_Response(message="Job posting not found.", status=404)

        user = request.env.user
        active = request.env["hr.applicant"].sudo().with_context(
            active_test=False,
        ).search(
            [
                ("candidate_user_id", "=", user.id),
                ("job_id", "=", job.id),
                ("active", "=", True),
                ("refuse_reason_id", "=", False),
                ("pipeline_status", "!=", "rejected"),
            ],
            limit=1,
        )
        if active:
            return return_Response(
                message="You have already applied to this role.",
                status=400,
                data={"record": _slim_for_portal(_serialize(active))},
            )

        unblock_at = _reapply_blocked_until(user.id, job.id)
        if unblock_at:
            return return_Response(
                message=(
                    "You can reapply to this role after %s."
                    % unblock_at.strftime("%B %d, %Y")
                ),
                status=409,
                data={"reapplyBlockedUntil": _iso(unblock_at)},
            )

        partner = user.partner_id
        vals = {
            "candidate_user_id": user.id,
            "job_id": job.id,
            "partner_name": partner.name or user.login,
            "email_from": partner.email or user.login,
            "partner_phone": partner.mobile or partner.phone or "",
            "status": "resume_screening_in_progress",
        }
        raw_expected_ctc = body.get("expectedCTC")
        if raw_expected_ctc is None:
            raw_expected_ctc = body.get("expected_ctc")
        if raw_expected_ctc not in (None, ""):
            try:
                expected_ctc = float(raw_expected_ctc)
            except (TypeError, ValueError):
                expected_ctc = -1.0
            if expected_ctc < 0:
                return return_Response(
                    message="`expectedCTC` must be a non-negative number.",
                    status=400,
                )
            vals["expected_ctc"] = expected_ctc
        if job.department_id:
            vals["department_id"] = job.department_id.id
        applicant = request.env["hr.applicant"].sudo().create(vals)
        return return_Response(
            message="Application submitted successfully.",
            status=200,
            data={"record": _slim_for_portal(_serialize(applicant))},
        )

    @http.route(
        BASE + "/profile", type="http", auth="none",
        methods=["PATCH", "POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    def candidate_me_profile(self, **kwargs):
        body, err = _read_json_body()
        if err is not None:
            return err

        applicant = _current_application(_applications_for_user())
        if not applicant:
            return return_Response(
                message="No candidate application found for the logged-in user.",
                status=404,
            )

        errors = []

        for req_key in _PROFILE_REQUIRED:
            raw = body.get(req_key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                errors.append(f"{req_key}: required.")

        vals = {}
        for form_key, (odoo_field, kind) in _PROFILE_WRITABLE.items():
            if form_key not in body:
                continue
            coerced, msg = _validate_and_coerce(body[form_key], kind)
            if msg:
                errors.append(f"{form_key}: {msg}")
                continue
            if coerced is None:
                continue
            if odoo_field in applicant._fields:
                vals[odoo_field] = coerced

        if body.get("experienceType") == "experienced":
            for req_when_exp in ("experienceYears", "currentCompany", "currentCTC", "expectedCTC", "noticePeriod"):
                val = body.get(req_when_exp)
                if val is None or (isinstance(val, str) and not val.strip()):
                    errors.append(f"{req_when_exp}: required for experienced candidates.")

        if errors:
            return return_Response(
                message="Please fix the errors below.",
                status=400,
                errors=errors,
            )

        if vals:
            applicant.sudo().write(vals)

        user = applicant.candidate_user_id
        partner = user.partner_id if user else None

        # ponytail: mirror email to res.users so /get_logged_user_details reflects it.
        # Kept in the controller — a hr.applicant write hook would also fire on
        # admin edits and change login identity unexpectedly.
        if user and "email_from" in vals:
            user.sudo().write({"email": vals["email_from"]})

        if partner:
            partner_vals = {}
            for form_key, partner_field in _PARTNER_STR_FIELDS.items():
                if form_key in body and body[form_key] is not None and partner_field in partner._fields:
                    partner_vals[partner_field] = str(body[form_key]).strip()
            if body.get("profilePic") and "profile_url" in partner._fields:
                # ponytail: reuse the S3 helper from api_auth_gateway rather than
                # duplicating base64→S3 upload logic.
                from odoo.addons.api_auth_gateway.controllers.main import generate_s3_link
                s3 = request.env["s3.connector"].sudo().search([], limit=1)
                raw = body["profilePic"]
                partner_vals["profile_url"] = (
                    raw if s3 and f"{s3.cdn_url}" in raw
                    else generate_s3_link(raw, uid=user.id)
                )
            if partner_vals:
                partner.sudo().write(partner_vals)

        notes = ["Name is not editable here — value ignored."] if "fullName" in body else []

        return return_Response(
            message="Profile updated.",
            status=200,
            data={**_overview_payload(), "notes": notes},
        )


__all__ = (
    "EtharaCandidateMeApi",
    "_serialize_position",
    "_serialize_college",
    "_serialize_contract",
)
