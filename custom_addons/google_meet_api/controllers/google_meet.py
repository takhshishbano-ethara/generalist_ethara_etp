import base64
import functools
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlparse

import requests

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

CONFIG_BASE = "/api/v1/google_meet/config"
AUTH_BASE = "/api/v1/google_meet/authenticate"
REFRESH_BASE = "/api/v1/google_meet/refresh_token"
EVENTS_BASE = "/api/v1/google_meet/events"
INTERVIEWS_BASE = "/api/v1/google_meet/interviews"
INTERVIEWEES_BASE = "/api/v1/google_meet/interviewees"
INTERVIEWERS_BASE = "/api/v1/google_meet/interviewers"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(html):
    if not html:
        return None
    text = _HTML_TAG_RE.sub(" ", html)
    text = unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def _interviewee_dict(applicant, assessment):
    job = applicant.job_id
    return {
        "id": applicant.id,
        "name": applicant.partner_name or applicant.name or "",
        "email": applicant.email_from or "",
        "phone": applicant.partner_phone or "",
        "job": {
            "id": job.id,
            "name": job.name,
            "department": (
                {"id": job.department_id.id, "name": job.department_id.name}
                if job.department_id else None
            ),
            "description_html": job.description or None,
            "description_text": _html_to_text(job.description),
        } if job else None,
        "resume": {
            "url": applicant.resume_url or None,
            "score": getattr(applicant, "resume_score", None) or None,
            "recommendation": applicant.resume_recommendation or None,
        },
        "assessment": {
            "id": assessment.id,
            "template": assessment.template_id.name if assessment.template_id else None,
            "final_score": assessment.final_score,
            "pass_mark_percent": assessment.pass_mark_percent,
            "submitted_at": _iso(assessment.submitted_at),
        } if assessment else None,
    }

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def _coerce_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value, default=None):
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


def _paginate(params):
    page = max(1, _coerce_int(params.get("page"), 1))
    limit = min(max(1, _coerce_int(params.get("limit"), DEFAULT_LIMIT)), MAX_LIMIT)
    return page, limit, (page - 1) * limit


def _pagination_block(total, page, limit):
    total_pages = (total + limit - 1) // limit if total else 0
    return {
        "total_records": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


def _read_json_body():
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


def _parse_datetime(raw, label, required=True):
    if raw in (None, "", False):
        if required:
            return None, return_Response(
                message=f"{label} is required.",
                status=400,
                errors=[f"{label} is required."],
            )
        return None, None
    text = str(raw).strip()
    try:
        if "T" in text:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        msg = (
            f"Invalid {label} '{raw}'. Expected ISO-8601 "
            "(e.g. 2026-07-20T13:30:00Z) or 'YYYY-MM-DD HH:MM:SS'."
        )
        return None, return_Response(message=msg, status=400, errors=[msg])
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value, None


def _handle_errors(func):
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
        except Exception:  # noqa: BLE001
            request.env.cr.rollback()
            _logger.exception(
                "google_meet_api: unhandled error in %s", func.__name__,
            )
            return return_Response(
                message="Internal server error.",
                status=500,
                errors=["Internal server error."],
            )

    return wrapper


def _resolve_attendees(items):
    Partner = request.env["res.partner"].sudo()
    ids = []
    for item in items or []:
        if item in (None, "", False):
            continue
        if isinstance(item, bool):
            continue
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            pid = int(item)
            if Partner.browse(pid).exists():
                ids.append(pid)
            continue
        if isinstance(item, str) and "@" in item:
            email = item.strip()
            partner = Partner.search([("email", "=ilike", email)], limit=1)
            if not partner:
                partner = Partner.create({"name": email, "email": email})
            ids.append(partner.id)
    return ids


def _employee_partner(employee_id):
    if not employee_id:
        return None
    Employee = request.env.get("hr.employee")
    if Employee is None:
        return None
    emp = Employee.sudo().browse(employee_id).exists()
    if not emp:
        return None
    if hasattr(emp, "work_contact_id") and emp.work_contact_id:
        return emp.work_contact_id
    if emp.user_id and emp.user_id.partner_id:
        return emp.user_id.partner_id
    return None


def _find_or_create_alarm(minutes):
    Alarm = request.env["calendar.alarm"].sudo()
    alarm = Alarm.search([
        ("alarm_type", "=", "email"),
        ("duration", "=", minutes),
        ("interval", "=", "minutes"),
    ], limit=1)
    if not alarm:
        alarm = Alarm.create({
            "name": f"{minutes} minutes before (email)",
            "alarm_type": "email",
            "duration": minutes,
            "interval": "minutes",
        })
    return alarm


def _fetch_resume_attachment(candidate, event):
    url = candidate.resume_url
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Resume fetch failed for %s: %s", url, exc)
        return False

    filename = urlparse(url).path.rsplit("/", 1)[-1] or "resume.pdf"
    name = candidate.partner_name or candidate.name or "candidate"
    display = f"{name} - resume.pdf" if filename.lower().endswith(".pdf") else filename
    return request.env["ir.attachment"].sudo().create({
        "name": display,
        "datas": base64.b64encode(resp.content),
        "res_model": "calendar.event",
        "res_id": event.id,
        "mimetype": resp.headers.get("Content-Type", "application/pdf"),
    })


def _company():
    return request.env.company


def _iso(value):
    return value.isoformat() if value else None


def _company_config_dict(company):
    return {
        "company_id": company.id,
        "company_name": company.name,
        "client_id": company.hangout_client_id or None,
        "client_secret_set": bool(company.hangout_client_secret),
        "redirect_uri": company.hangout_redirect_uri or None,
        "has_access_token": bool(company.hangout_company_access_token),
        "has_refresh_token": bool(company.hangout_company_refresh_token),
        "access_token_expiry": _iso(
            company.hangout_company_access_token_expiry,
        ),
        "is_authenticated": bool(
            company.hangout_company_access_token
            and company.hangout_company_refresh_token
        ),
    }


def _event_or_404(eid):
    rec = request.env["calendar.event"].sudo().browse(eid).exists()
    if not rec:
        return None, return_Response(
            message="Event not found.",
            status=404,
            errors=["Event not found."],
        )
    return rec, None


def _event_dict(rec):
    return {
        "id": rec.id,
        "name": rec.name,
        "start": _iso(rec.start),
        "stop": _iso(rec.stop),
        "duration": rec.duration or 0.0,
        "allday": bool(rec.allday),
        "location": rec.location or None,
        "description": rec.description or None,
        "user_id": (
            {"id": rec.user_id.id, "name": rec.user_id.name}
            if rec.user_id
            else None
        ),
        "partner_ids": [
            {"id": p.id, "name": p.name, "email": p.email or None}
            for p in rec.partner_ids
        ],
        "is_google_meet": bool(rec.is_google_meet),
        "google_meet_url": rec.google_meet_url or None,
        "google_meet_code": rec.google_meet_code or None,
        "google_event_id": rec.google_event_id or None,
        "create_date": _iso(rec.create_date),
        "write_date": _iso(rec.write_date),
    }


class GoogleMeetApi(http.Controller):

    @http.route(
        CONFIG_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_config_get(self, **kwargs):
        return return_Response(
            message="OK",
            status=200,
            data={"config": _company_config_dict(_company())},
        )

    @http.route(
        CONFIG_BASE,
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_config_set(self, **kwargs):
        data, err = _read_json_body()
        if err is not None:
            return err

        vals = {}
        if "client_id" in data:
            vals["hangout_client_id"] = (
                (data.get("client_id") or "").strip() or False
            )
        if "client_secret" in data:
            vals["hangout_client_secret"] = (
                (data.get("client_secret") or "").strip() or False
            )
        if "redirect_uri" in data:
            vals["hangout_redirect_uri"] = (
                (data.get("redirect_uri") or "").strip() or False
            )
        if not vals:
            msg = (
                "Provide at least one of: client_id, client_secret, "
                "redirect_uri."
            )
            return return_Response(message=msg, status=400, errors=[msg])

        _company().sudo().write(vals)
        return return_Response(
            message="Configuration saved.",
            status=200,
            data={"config": _company_config_dict(_company())},
        )

    @http.route(
        AUTH_BASE,
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_authenticate(self, **kwargs):
        action = _company().sudo().google_meet_company_authenticate()
        return return_Response(
            message="OK",
            status=200,
            data={
                "auth_url": action.get("url"),
                "instructions": (
                    "Open auth_url in a browser. After the user grants access, "
                    "Google redirects to the configured redirect_uri "
                    "(/google_meet_authentication) which stores the tokens. "
                    "Poll GET /api/v1/google_meet/config to verify "
                    "is_authenticated=true."
                ),
            },
        )

    @http.route(
        REFRESH_BASE,
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_refresh_token(self, **kwargs):
        _company().sudo().google_meet_company_refresh_token()
        return return_Response(
            message="Token refreshed.",
            status=200,
            data={"config": _company_config_dict(_company())},
        )

    @http.route(
        EVENTS_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_event_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        domain = []
        is_meet = _coerce_bool(params.get("is_google_meet"))
        if is_meet is not None:
            domain.append(("is_google_meet", "=", is_meet))
        if params.get("user_id"):
            uid = _coerce_int(params["user_id"])
            if uid is not None:
                domain.append(("user_id", "=", uid))
        if params.get("from_date"):
            start, err = _parse_datetime(params["from_date"], "from_date")
            if err is not None:
                return err
            domain.append(("start", ">=", start))
        if params.get("to_date"):
            end, err = _parse_datetime(params["to_date"], "to_date")
            if err is not None:
                return err
            domain.append(("start", "<=", end))

        Event = request.env["calendar.event"].sudo()
        total = Event.search_count(domain)
        records = Event.search(
            domain, offset=offset, limit=limit, order="start desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "events": [_event_dict(rec) for rec in records],
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(
        EVENTS_BASE,
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_event_create(self, **kwargs):
        data, err = _read_json_body()
        if err is not None:
            return err

        name = (data.get("name") or "").strip()
        if not name:
            msg = "name is required."
            return return_Response(message=msg, status=400, errors=[msg])

        allday = _coerce_bool(data.get("allday"), False)
        start, err = _parse_datetime(data.get("start"), "start")
        if err is not None:
            return err
        stop, err = _parse_datetime(data.get("stop"), "stop")
        if err is not None:
            return err

        vals = {
            "name": name,
            "start": start,
            "stop": stop,
            "allday": allday,
            "is_google_meet": _coerce_bool(data.get("is_google_meet"), True),
        }
        if data.get("location"):
            vals["location"] = data["location"]
        if data.get("description"):
            vals["description"] = data["description"]
        if data.get("user_id"):
            uid = _coerce_int(data["user_id"])
            if uid is not None:
                vals["user_id"] = uid
        if isinstance(data.get("partner_ids"), list):
            pids = [
                _coerce_int(x)
                for x in data["partner_ids"]
                if _coerce_int(x) is not None
            ]
            if pids:
                vals["partner_ids"] = [(6, 0, pids)]
        if isinstance(data.get("duration"), (int, float)):
            vals["duration"] = float(data["duration"])

        event = request.env["calendar.event"].sudo().create(vals)
        return return_Response(
            message="Event created.",
            status=200,
            data={"event": _event_dict(event)},
        )

    @http.route(
        EVENTS_BASE + "/<int:eid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_event_detail(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"event": _event_dict(rec)},
        )

    @http.route(
        EVENTS_BASE + "/<int:eid>",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_event_update(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        data, err = _read_json_body()
        if err is not None:
            return err

        vals = {}
        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                msg = "name cannot be empty."
                return return_Response(message=msg, status=400, errors=[msg])
            vals["name"] = name
        if "start" in data:
            start, err = _parse_datetime(data["start"], "start", required=False)
            if err is not None:
                return err
            vals["start"] = start
        if "stop" in data:
            stop, err = _parse_datetime(data["stop"], "stop", required=False)
            if err is not None:
                return err
            vals["stop"] = stop
        if "allday" in data:
            vals["allday"] = _coerce_bool(data["allday"], False)
        if "location" in data:
            vals["location"] = data.get("location") or False
        if "description" in data:
            vals["description"] = data.get("description") or False
        if "user_id" in data:
            uid = _coerce_int(data["user_id"])
            vals["user_id"] = uid if uid is not None else False
        if "partner_ids" in data and isinstance(data["partner_ids"], list):
            pids = [
                _coerce_int(x)
                for x in data["partner_ids"]
                if _coerce_int(x) is not None
            ]
            vals["partner_ids"] = [(6, 0, pids)]
        if "duration" in data and isinstance(data["duration"], (int, float)):
            vals["duration"] = float(data["duration"])
        if "is_google_meet" in data:
            vals["is_google_meet"] = _coerce_bool(
                data["is_google_meet"], False,
            )

        if not vals:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])

        rec.write(vals)
        return return_Response(
            message="Event updated.",
            status=200,
            data={"event": _event_dict(rec)},
        )

    @http.route(
        EVENTS_BASE + "/<int:eid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_event_delete(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Event deleted.", status=200)

    @http.route(
        INTERVIEWEES_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_interviewees_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        Assessment = request.env["etp.applicant.assessment"].sudo()
        latest_pass_ids = Assessment.search([("result", "=", "pass")], order="id desc")

        seen_applicant_ids = set()
        pairs = []
        for a in latest_pass_ids:
            aid = a.applicant_id.id if a.applicant_id else None
            if not aid or aid in seen_applicant_ids:
                continue
            seen_applicant_ids.add(aid)
            pairs.append((a.applicant_id, a))

        job_id = _coerce_int(params.get("job_id"))
        if job_id:
            pairs = [
                (app, a) for (app, a) in pairs
                if app.job_id and app.job_id.id == job_id
            ]

        search_q = (params.get("search") or "").strip().lower()
        if search_q:
            pairs = [
                (app, a) for (app, a) in pairs
                if search_q in (app.partner_name or "").lower()
                or search_q in (app.email_from or "").lower()
                or search_q in (app.name or "").lower()
            ]

        total = len(pairs)
        window = pairs[offset:offset + limit]

        return return_Response(
            message="OK",
            status=200,
            data={
                "interviewees": [_interviewee_dict(app, a) for (app, a) in window],
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(
        INTERVIEWERS_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_interviewers_list(self, **kwargs):
        params = request.params or {}
        page, limit, offset = _paginate(params)

        domain = [("active", "=", True), ("work_email", "!=", False)]
        dept_id = _coerce_int(params.get("department_id"))
        if dept_id:
            domain.append(("department_id", "=", dept_id))
        search_q = (params.get("search") or "").strip()
        if search_q:
            domain += [
                "|", "|",
                ("name", "ilike", search_q),
                ("work_email", "ilike", search_q),
                ("job_title", "ilike", search_q),
            ]

        Employee = request.env["hr.employee"].sudo()
        total = Employee.search_count(domain)
        records = Employee.search(domain, offset=offset, limit=limit, order="name")

        interviewers = []
        for emp in records:
            partner = (
                getattr(emp, "work_contact_id", False)
                or (emp.user_id.partner_id if emp.user_id else False)
            )
            interviewers.append({
                "id": emp.id,
                "name": emp.name,
                "email": emp.work_email or None,
                "partner_id": partner.id if partner else None,
                "department": (
                    {"id": emp.department_id.id, "name": emp.department_id.name}
                    if emp.department_id else None
                ),
                "job_title": emp.job_title or None,
            })
        return return_Response(
            message="OK",
            status=200,
            data={
                "interviewers": interviewers,
                "pagination": _pagination_block(total, page, limit),
            },
        )

    @http.route(
        INTERVIEWS_BASE + "/<int:eid>/reschedule",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_interview_reschedule(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        data, err = _read_json_body()
        if err is not None:
            return err

        new_start, err = _parse_datetime(data.get("scheduledAt"), "scheduledAt")
        if err is not None:
            return err

        duration = _coerce_int(data.get("durationMinutes"), 60)
        if duration is None or duration <= 0:
            msg = "durationMinutes must be a positive integer."
            return return_Response(message=msg, status=400, errors=[msg])

        old_start = rec.start
        stop = new_start + timedelta(minutes=duration)
        rec.write({
            "start": new_start,
            "stop": stop,
            "duration": duration / 60.0,
        })

        notify = _coerce_bool(data.get("notifyAttendees"), True)
        if notify and rec.attendee_ids:
            reason = (data.get("reason") or "").strip()
            body = (
                f"<p><strong>Interview rescheduled.</strong></p>"
                f"<p>Old: {old_start} → New: {new_start}</p>"
            )
            if reason:
                body += f"<p><strong>Reason:</strong> {reason}</p>"
            rec.message_post(
                body=body,
                partner_ids=rec.partner_ids.ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )
            rec.attendee_ids.with_context(
                no_mail_to_attendees=False,
                mail_notify_force_send=True,
            )._send_invitation_emails()

        return return_Response(
            message="Interview rescheduled.",
            status=200,
            data={"event": _event_dict(rec)},
        )

    @http.route(
        INTERVIEWS_BASE + "/<int:eid>/cancel",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_interview_cancel(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        data, err = _read_json_body()
        if err is not None:
            return err

        reason = (data.get("reason") or "other").strip()
        comment = (data.get("comment") or "").strip()
        notify = _coerce_bool(data.get("notifyAttendees"), True)

        if notify and rec.attendee_ids:
            body = (
                f"<p><strong>Interview cancelled.</strong></p>"
                f"<p><strong>Reason:</strong> {reason}</p>"
            )
            if comment:
                body += f"<p>{comment}</p>"
            rec.message_post(
                body=body,
                partner_ids=rec.partner_ids.ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )
        rec.unlink()
        return return_Response(message="Interview cancelled.", status=200)

    @http.route(
        INTERVIEWS_BASE + "/schedule",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_interview_schedule(self, **kwargs):
        data, err = _read_json_body()
        if err is not None:
            return err

        candidate_id = _coerce_int(data.get("candidateId") or data.get("applicantId"))
        candidate = None
        if candidate_id:
            candidate = request.env["hr.applicant"].sudo().browse(candidate_id).exists()
            if not candidate:
                msg = f"Candidate (hr.applicant) id={candidate_id} not found."
                return return_Response(message=msg, status=404, errors=[msg])

        title = (data.get("title") or "").strip()
        if not title and candidate:
            title = f"Interview - {candidate.partner_name or candidate.name or 'Candidate'}"
        if not title:
            msg = "title is required (or provide candidateId to auto-generate)."
            return return_Response(message=msg, status=400, errors=[msg])

        start, err = _parse_datetime(data.get("scheduledAt"), "scheduledAt")
        if err is not None:
            return err

        duration_minutes = _coerce_int(data.get("durationMinutes"), 60)
        if duration_minutes is None or duration_minutes <= 0:
            msg = "durationMinutes must be a positive integer."
            return return_Response(message=msg, status=400, errors=[msg])
        stop = start + timedelta(minutes=duration_minutes)

        mode = (data.get("mode") or "online").strip().lower()
        is_online = mode == "online"

        if not is_online and not (data.get("location") or "").strip():
            msg = "location is required when mode is 'offline'."
            return return_Response(message=msg, status=400, errors=[msg])

        partner_ids = _resolve_attendees(
            data.get("interviewers") or data.get("attendees"),
        )

        if candidate:
            Partner = request.env["res.partner"].sudo()
            partner = candidate.partner_id
            if not partner and candidate.email_from:
                partner = Partner.search(
                    [("email", "=ilike", candidate.email_from)], limit=1,
                )
                if not partner:
                    partner = Partner.create({
                        "name": candidate.partner_name or candidate.email_from,
                        "email": candidate.email_from,
                    })
            if partner:
                partner_ids.append(partner.id)

        employee_id = _coerce_int(data.get("employeeId"))
        if employee_id and _coerce_bool(data.get("inviteEmployee"), True):
            emp_partner = _employee_partner(employee_id)
            if emp_partner:
                partner_ids.append(emp_partner.id)

        partner_ids = list(dict.fromkeys(partner_ids))

        job_id = _coerce_int(data.get("jobId"))
        job = None
        if job_id:
            job = request.env["hr.job"].sudo().browse(job_id).exists()
        elif candidate and candidate.job_id:
            job = candidate.job_id

        description_parts = []
        if data.get("notes"):
            description_parts.append(f"<p>{data['notes']}</p>")
        elif data.get("description"):
            description_parts.append(data["description"])
        if job:
            description_parts.append(
                f"<p><strong>Job Position:</strong> {job.name}</p>"
            )
            if job.description:
                description_parts.append(
                    f"<p><strong>Job Description:</strong></p>{job.description}"
                )
        if candidate and candidate.resume_url:
            description_parts.append(
                f'<p><strong>Resume:</strong> '
                f'<a href="{candidate.resume_url}" target="_blank">'
                f'Download candidate resume</a></p>'
            )
        if data.get("jobLink"):
            description_parts.append(
                f'<p><strong>Link:</strong> '
                f'<a href="{data["jobLink"]}" target="_blank">{data["jobLink"]}</a></p>'
            )

        vals = {
            "name": title,
            "start": start,
            "stop": stop,
            "duration": duration_minutes / 60.0,
            "allday": False,
            "is_google_meet": is_online,
            "description": "".join(description_parts) or False,
        }
        if data.get("location"):
            vals["location"] = data["location"]
        if partner_ids:
            vals["partner_ids"] = [(6, 0, partner_ids)]

        reminder_minutes = _coerce_int(data.get("reminderMinutes"), 15)
        if reminder_minutes and reminder_minutes > 0:
            alarm = _find_or_create_alarm(reminder_minutes)
            if alarm:
                vals["alarm_ids"] = [(6, 0, [alarm.id])]

        event = request.env["calendar.event"].sudo().with_context(
            no_mail_to_attendees=True,
            mail_create_nolog=True,
        ).create(vals)

        if event.google_meet_url and not event.videocall_location:
            event.videocall_location = event.google_meet_url

        if event.attendee_ids:
            event.attendee_ids.with_context(
                no_mail_to_attendees=False,
                mail_notify_force_send=True,
            )._send_invitation_emails()

        Attachment = request.env["ir.attachment"].sudo()
        event_attachments = Attachment

        if candidate:
            resume_att = _fetch_resume_attachment(candidate, event)
            if resume_att:
                event_attachments |= resume_att

        for att in data.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            content = att.get("data") or att.get("content")
            name = (att.get("name") or att.get("filename") or "").strip()
            if not content or not name:
                continue
            try:
                event_attachments |= Attachment.create({
                    "name": name,
                    "datas": content,
                    "res_model": "calendar.event",
                    "res_id": event.id,
                    "mimetype": att.get("mimetype") or "application/octet-stream",
                })
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Attachment create failed for '%s': %s", name, exc)

        if event_attachments and partner_ids:
            event.message_post(
                body="Interview materials attached.",
                attachment_ids=event_attachments.ids,
                partner_ids=partner_ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )

        warning = None
        if is_online and not event.google_meet_url:
            warning = (
                "Interview created but Google Meet link could not be generated. "
                "Check Google authentication (GET /api/v1/google_meet/config)."
            )

        return return_Response(
            message="Interview scheduled.",
            status=200,
            data={
                "event": _event_dict(event),
                "candidate": (
                    {"id": candidate.id, "name": candidate.partner_name or candidate.name}
                    if candidate else None
                ),
                "job": ({"id": job.id, "name": job.name} if job else None),
                "warning": warning,
            },
        )
