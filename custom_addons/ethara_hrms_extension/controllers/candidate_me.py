import json
import logging
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
    "fullName": ("partner_name", "str"),
    "phone": ("partner_phone", "str"),
    "gender": ("gender", "str"),
    "dateOfBirth": ("birthday", "date"),
    "maritalStatus": ("marital", "str"),
    "experienceType": ("experience", "str"),
    "experienceYears": ("experience_years", "float"),
    "currentCompany": ("current_company", "str"),
    "currentCTC": ("current_ctc", "float"),
    "expectedCTC": ("expected_ctc", "float"),
    "noticePeriod": ("notice_period_days", "int"),
    "collegeId": ("college_id", "many2one"),
}


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


def _coerce_value(raw, kind):
    if kind == "str":
        if raw is None:
            return "", True
        return (str(raw).strip(), True)
    if kind == "int":
        try:
            return int(raw or 0), True
        except (TypeError, ValueError):
            return None, False
    if kind == "float":
        try:
            return float(raw or 0), True
        except (TypeError, ValueError):
            return None, False
    if kind == "date":
        parsed = _coerce_date(raw)
        return parsed, parsed is not None or raw in (None, "")
    if kind == "many2one":
        try:
            val = int(raw) if raw not in (None, "") else False
            return val, True
        except (TypeError, ValueError):
            return None, False
    return raw, True


def _overview_payload():
    user = request.env.user
    applications = _applications_for_user()
    current = _current_application(applications)
    return {
        "currentApplication": _serialize(current) if current else None,
        "applications": [_serialize(a) for a in applications],
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
                data={"record": _serialize(active)},
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
            data={"record": _serialize(applicant)},
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

        applications = _applications_for_user()
        applicant = _current_application(applications)
        if not applicant:
            return return_Response(
                message="No candidate application found for the logged-in user.",
                status=404,
            )

        vals = {}
        rejected = []
        for key, (dfield, kind) in _PROFILE_WRITABLE.items():
            if key not in body:
                continue
            if dfield not in applicant._fields:
                continue
            coerced, ok = _coerce_value(body[key], kind)
            if not ok:
                rejected.append(key)
                continue
            vals[dfield] = coerced

        if rejected:
            return return_Response(
                message="Invalid value for one or more fields.",
                status=400,
                errors=[f"invalid value for `{k}`" for k in rejected],
            )

        if vals:
            applicant.sudo().write(vals)

        return return_Response(
            message="Profile updated.", status=200,
            data=_overview_payload(),
        )


__all__ = (
    "EtharaCandidateMeApi",
    "_serialize_position",
    "_serialize_college",
    "_serialize_contract",
)
