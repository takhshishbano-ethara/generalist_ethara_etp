import base64
import functools
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlparse

import requests
from markupsafe import Markup

from odoo import SUPERUSER_ID, http
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
CANDIDATES_BASE = "/api/v1/google_meet/candidates"

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
    stage = applicant.stage_id
    return {
        "id": applicant.id,
        "name": applicant.partner_name or applicant.email_from or f"#{applicant.id}",
        "email": applicant.email_from or "",
        "phone": applicant.partner_phone or "",
        "stage": ({"id": stage.id, "name": stage.name} if stage else None),
        "status": applicant.status if "status" in applicant._fields else None,
        "job": {
            "id": job.id,
            "name": job.name,
            "department": (
                {"id": job.department_id.id, "name": job.department_id.name}
                if job.department_id else None
            ),
            "description_html": job.description or None,
            "description_text": _html_to_text(job.description),
            "requirements_html": (getattr(job, "requirements", None) or None),
            "requirements_text": _html_to_text(getattr(job, "requirements", None)),
        } if job else None,
        "resume": {
            "url": applicant.resume_url or None,
            "score": getattr(applicant, "resume_score", None) or None,
            "recommendation": applicant.resume_recommendation or None,
            "summary": getattr(applicant, "resume_summary", None) or None,
            "strengths": getattr(applicant, "resume_strengths", None) or None,
            "gaps": getattr(applicant, "resume_gaps", None) or None,
            "screened_at": _iso(getattr(applicant, "resume_screened_at", None)),
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
        ("alarm_type", "=", "notification"),
        ("duration", "=", minutes),
        ("interval", "=", "minutes"),
    ], limit=1)
    if not alarm:
        alarm = Alarm.create({
            "name": f"{minutes} minutes before",
            "alarm_type": "notification",
            "duration": minutes,
            "interval": "minutes",
        })
    return alarm


def _fetch_resume_attachment(candidate, event):
    url = candidate.resume_url
    if not url:
        return False

    content = None
    mimetype = "application/pdf"
    try:
        out = subprocess.run(
            ["curl", "-fsSL", "--max-time", "15", url],
            capture_output=True, check=True,
        )
        content = out.stdout
    except Exception as exc:  # noqa: BLE001 - fall through to requests
        _logger.warning("Resume fetch via curl failed: %s. Trying requests.", exc)
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            content = resp.content
            mimetype = resp.headers.get("Content-Type", "application/pdf")
        except Exception as req_exc:  # noqa: BLE001
            _logger.warning("Resume fetch via requests also failed: %s", req_exc)
            return False

    if not content:
        return False

    filename = urlparse(url).path.rsplit("/", 1)[-1] or "resume.pdf"
    name = candidate.partner_name or candidate.name or "candidate"
    display = f"{name} - resume.pdf" if filename.lower().endswith(".pdf") else filename
    return request.env["ir.attachment"].sudo().create({
        "name": display,
        "datas": base64.b64encode(content),
        "res_model": "calendar.event",
        "res_id": event.id,
        "mimetype": mimetype,
    })


def _get_configured_from_email():
    server = request.env["ir.mail_server"].sudo().search(
        [("active", "=", True)], limit=1, order="sequence, id",
    )
    if server and server.smtp_user:
        return server.smtp_user
    company = request.env.company
    return company.email or (company.partner_id.email if company.partner_id else False)


def _queue_invitation_emails(attendees, extra_attachment_ids=None):
    template = request.env.ref(
        "google_meet_api.mail_template_pi_invitation",
        raise_if_not_found=False,
    ) or request.env.ref(
        "calendar.calendar_template_meeting_invitation",
        raise_if_not_found=False,
    )
    if not template:
        return
    ta_partner = request.env.user.partner_id
    non_ta = attendees.filtered(lambda a: a.partner_id.id != ta_partner.id)
    if not non_ta:
        return
    event_ids = non_ta.mapped("event_id").ids
    non_ta.with_user(SUPERUSER_ID).with_context(
        no_mail_to_attendees=False,
    )._notify_attendees(template, force_send=False)
    if event_ids:
        new_mails = request.env["mail.mail"].sudo().search([
            ("state", "=", "outgoing"),
            ("mail_message_id.res_id", "in", event_ids),
            ("mail_message_id.model", "=", "calendar.event"),
        ], order="id")
        if new_mails:
            write_vals = {"auto_delete": False}
            from_email = _get_configured_from_email()
            if from_email:
                write_vals["email_from"] = from_email
            if extra_attachment_ids:
                write_vals["attachment_ids"] = [(4, aid) for aid in extra_attachment_ids]
            new_mails.write(write_vals)
            if ta_partner.email:
                new_mails[0].email_cc = ta_partner.email


def _cancel_event_and_notify(event, reason, comment=None, notify=True):
    if notify and event.attendee_ids:
        _queue_cancellation_emails(event, reason, comment)
    if event.google_event_id:
        try:
            event._delete_google_meet(event.google_event_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Google Meet delete failed on cancel: %s", exc)
    event.with_context(dont_notify=True).write({
        "active": False,
        "google_meet_url": False,
        "google_meet_code": False,
        "google_event_id": False,
        "videocall_location": False,
        "need_sync": False,
    })


def _queue_cancellation_emails(event, reason, comment=None):
    ics_att_id = None
    ics_map = event._get_ics_file() or {}
    ics_bytes = ics_map.get(event.id)
    if ics_bytes:
        ics_str = ics_bytes.decode("utf-8", errors="ignore")
        if "STATUS:" in ics_str:
            ics_str = re.sub(r"STATUS:[A-Z]+", "STATUS:CANCELLED", ics_str)
        else:
            ics_str = ics_str.replace("BEGIN:VEVENT", "BEGIN:VEVENT\r\nSTATUS:CANCELLED", 1)
        if "METHOD:" in ics_str:
            ics_str = re.sub(r"METHOD:[A-Z]+", "METHOD:CANCEL", ics_str)
        else:
            ics_str = ics_str.replace("BEGIN:VCALENDAR", "BEGIN:VCALENDAR\r\nMETHOD:CANCEL", 1)
        ics_att_id = request.env["ir.attachment"].sudo().create({
            "name": "cancelled.ics",
            "datas": base64.b64encode(ics_str.encode("utf-8")),
            "mimetype": "text/calendar; method=CANCEL",
            "res_model": "mail.compose.message",
            "res_id": 0,
        }).id

    cancel_template = request.env.ref(
        "google_meet_api.mail_template_pi_cancellation",
        raise_if_not_found=False,
    )
    if cancel_template:
        ctx = {"cancel_reason": reason or "other", "cancel_comment": comment or ""}
        tmpl = cancel_template.sudo().with_context(**ctx)
        body = tmpl._render_field("body_html", event.ids, compute_lang=True)[event.id]
        subject = tmpl._render_field("subject", event.ids, compute_lang=True)[event.id]
    else:
        body = Markup(
            "<div><p><strong>Interview cancelled.</strong></p>"
            "<p><strong>Meeting:</strong> %s</p>"
            "<p><strong>Was scheduled for:</strong> %s</p>"
            "<p><strong>Reason:</strong> %s</p>%s"
            "<p><em>This meeting has been removed from your calendar.</em></p></div>"
        ) % (
            event.name or "Interview",
            str(event.start) if event.start else "",
            reason or "other",
            (Markup("<p><strong>Comment:</strong> %s</p>") % comment) if comment else Markup(""),
        )
        subject = f"Cancelled: {event.name}" if event.name else "Interview cancelled"

    author_partner_id = (
        event.user_id.partner_id.id
        if event.user_id and event.user_id.partner_id
        else request.env.user.partner_id.id
    )
    ta_partner = request.env.user.partner_id
    from_email = _get_configured_from_email()
    cc_email = ta_partner.email or False
    for attendee in event.attendee_ids:
        if not attendee.partner_id or attendee.partner_id.id == ta_partner.id:
            continue
        msg = event.with_user(SUPERUSER_ID).with_context(no_document=True).message_notify(
            partner_ids=attendee.partner_id.ids,
            subject=subject,
            body=body,
            attachment_ids=[ics_att_id] if ics_att_id else [],
            email_layout_xmlid="mail.mail_notification_light",
            author_id=author_partner_id,
            force_send=False,
            mail_auto_delete=False,
        )
        if msg:
            mails = request.env["mail.mail"].sudo().search([
                ("mail_message_id", "=", msg.id),
                ("state", "=", "outgoing"),
            ])
            if mails:
                write_vals = {}
                if from_email:
                    write_vals["email_from"] = from_email
                if cc_email:
                    write_vals["email_cc"] = cc_email
                if write_vals:
                    mails.write(write_vals)


def _active_upcoming_event_for(candidate_id):
    return request.env["calendar.event"].sudo().search(
        [
            ("candidate_id", "=", candidate_id),
            ("active", "=", True),
            ("stop", ">=", datetime.utcnow()),
            ("evaluation_submitted", "=", False),
        ],
        limit=1,
        order="start desc",
    )


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
    if (rec.candidate_id and rec.stop and rec.stop < datetime.utcnow()
            and rec.interview_status in ("upcoming", "in_progress")):
        rec.invalidate_recordset(["interview_status"])
        rec._compute_interview_status()
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
        "meet_link_note": (
            "This meeting link has expired — interview process is closed."
            if rec.candidate_id and rec.candidate_id.status in ("pi_selected", "pi_rejected")
            and not rec.google_meet_url
            else None
        ),
        "candidate_id": (
            {"id": rec.candidate_id.id, "name": rec.candidate_id.partner_name or rec.candidate_id.name}
            if rec.candidate_id else None
        ),
        "interview_round": rec.interview_round or 0,
        "interview_status": rec.interview_status or "upcoming",
        "rescheduled_count": rec.rescheduled_count or 0,
        "evaluation": {
            "technical_skills": rec.technical_skills_score or 0,
            "communication": rec.communication_score or 0,
            "problem_solving": rec.problem_solving_score or 0,
            "cultural_fit": rec.cultural_fit_score or 0,
            "attitude_motivation": rec.attitude_motivation_score or 0,
            "notes": rec.evaluation_notes or None,
            "overall_score": rec.overall_score or 0,
            "submitted": bool(rec.evaluation_submitted),
        },
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
        if params.get("candidate_id"):
            cid = _coerce_int(params["candidate_id"])
            if cid is not None:
                domain.append(("candidate_id", "=", cid))
        has_candidate = _coerce_bool(params.get("has_candidate"))
        if has_candidate is True:
            domain.append(("candidate_id", "!=", False))
        elif has_candidate is False:
            domain.append(("candidate_id", "=", False))
        if params.get("status"):
            domain.append(("interview_status", "=", params["status"].strip()))
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

        Event = request.env["calendar.event"].sudo().with_context(active_test=False)
        total = Event.search_count(domain)
        records = Event.search(
            domain, offset=offset, limit=limit, order="start desc",
        )
        FINALIZED = ("pi_selected", "pi_rejected", "pi_hold")
        deduped = []
        seen_finalized_candidates = set()
        for rec in records:
            cand = rec.candidate_id
            if cand and cand.status in FINALIZED:
                if cand.id in seen_finalized_candidates:
                    continue
                seen_finalized_candidates.add(cand.id)
            deduped.append(rec)
        return return_Response(
            message="OK",
            status=200,
            data={
                "events": [_event_dict(rec) for rec in deduped],
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

        for score_field in (
            "technical_skills_score",
            "communication_score",
            "problem_solving_score",
            "cultural_fit_score",
            "attitude_motivation_score",
        ):
            if score_field in data:
                sv = _coerce_int(data[score_field])
                if sv is None or not (0 <= sv <= 10):
                    msg = f"{score_field} must be an integer 0-10."
                    return return_Response(message=msg, status=400, errors=[msg])
                vals[score_field] = sv
        if "evaluation_notes" in data:
            vals["evaluation_notes"] = data.get("evaluation_notes") or False
        if "candidate_id" in data:
            cid = _coerce_int(data["candidate_id"])
            vals["candidate_id"] = cid if cid is not None else False

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
        EVENTS_BASE + "/<int:eid>/score",
        type="http", auth="none", methods=["POST", "PUT", "PATCH"], csrf=False, cors="*",
        readonly=False,
    )
    @validate_token
    @_handle_errors
    def google_meet_event_score(self, eid, **kwargs):
        rec, err = _event_or_404(eid)
        if err is not None:
            return err
        data, err = _read_json_body()
        if err is not None:
            return err

        vals = {}
        for score_field in (
            "technical_skills_score", "communication_score",
            "problem_solving_score", "cultural_fit_score",
            "attitude_motivation_score",
        ):
            if score_field in data:
                sv = _coerce_int(data[score_field])
                if sv is None or not (0 <= sv <= 10):
                    msg = f"{score_field} must be integer 0-10."
                    return return_Response(message=msg, status=400, errors=[msg])
                vals[score_field] = sv
        if "evaluation_notes" in data:
            vals["evaluation_notes"] = data.get("evaluation_notes") or False
        if not vals:
            msg = "Provide at least one score field or evaluation_notes."
            return return_Response(message=msg, status=400, errors=[msg])

        rec.sudo().with_context(dont_notify=True).write({**vals, "need_sync": False})
        return return_Response(
            message="Scores saved.",
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

        scheduled_candidate_ids = set(
            request.env["calendar.event"].sudo().search([
                ("candidate_id", "!=", False),
                ("active", "=", True),
                ("stop", ">=", datetime.utcnow()),
                ("evaluation_submitted", "=", False),
            ]).mapped("candidate_id.id")
        )
        SCHEDULABLE_STATUSES = ("assessment_passed", "pi_completed", "pi_hold")
        pairs = [
            (app, a) for (app, a) in pairs
            if app.id not in scheduled_candidate_ids
            and (app.status in SCHEDULABLE_STATUSES or not app.status)
        ]

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
        CANDIDATES_BASE + "/<int:cid>/interviews",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @_handle_errors
    def google_meet_candidate_interviews(self, cid, **kwargs):
        candidate = request.env["hr.applicant"].sudo().browse(cid).exists()
        if not candidate:
            msg = f"Candidate (hr.applicant) id={cid} not found."
            return return_Response(message=msg, status=404, errors=[msg])

        events = request.env["calendar.event"].sudo().with_context(active_test=False).search(
            [("candidate_id", "=", cid)],
            order="start asc",
        )
        now = datetime.utcnow()
        upcoming, past = [], []
        for ev in events:
            data = _event_dict(ev)
            if not ev.active or (ev.stop and ev.stop < now):
                past.append(data)
            else:
                data.pop("evaluation", None)
                upcoming.append(data)
        past.reverse()

        return return_Response(
            message="OK",
            status=200,
            data={
                "candidate": {
                    "id": candidate.id,
                    "name": candidate.partner_name or candidate.name,
                    "email": candidate.email_from or None,
                    "job": (
                        {"id": candidate.job_id.id, "name": candidate.job_id.name}
                        if candidate.job_id else None
                    ),
                    "resume_url": candidate.resume_url or None,
                },
                "upcoming": upcoming,
                "past": past,
                "total_rounds": len(events),
                "max_rounds": 5,
                "rounds_remaining": max(0, 5 - len(events)),
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

        stop = new_start + timedelta(minutes=duration)
        write_vals = {
            "start": new_start,
            "stop": stop,
            "duration": duration / 60.0,
            "rescheduled_count": (rec.rescheduled_count or 0) + 1,
        }

        reason = (data.get("reason") or "").strip()
        if reason:
            reason_html = Markup("<p><strong>Rescheduled — reason:</strong> %s</p>") % reason
            existing = rec.description or Markup("")
            if str(reason_html) not in str(existing):
                write_vals["description"] = reason_html + existing

        rec.write(write_vals)

        notify = _coerce_bool(data.get("notifyAttendees"), True)
        if notify and rec.attendee_ids:
            _queue_invitation_emails(rec.attendee_ids)

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

        _cancel_event_and_notify(
            rec,
            reason=(data.get("reason") or "other").strip(),
            comment=(data.get("comment") or "").strip() or None,
            notify=_coerce_bool(data.get("notifyAttendees"), True),
        )
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
        existing_to_cancel = None
        if candidate_id:
            candidate = request.env["hr.applicant"].sudo().browse(candidate_id).exists()
            if not candidate:
                msg = f"Candidate (hr.applicant) id={candidate_id} not found."
                return return_Response(message=msg, status=404, errors=[msg])
            if not _coerce_bool(data.get("allowConcurrent"), False):
                existing = _active_upcoming_event_for(candidate.id)
                if existing:
                    if not _coerce_bool(data.get("forceReplace"), False):
                        msg = (
                            f"Candidate already has an active interview scheduled "
                            f"(event_id={existing.id}, start={_iso(existing.start)}). "
                            f"Send 'forceReplace': true to auto-cancel it, "
                            f"or 'allowConcurrent': true to schedule a parallel round."
                        )
                        return return_Response(
                            message=msg, status=409, errors=[msg],
                            data={"existing_event_id": existing.id},
                        )
                    existing_to_cancel = existing

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
            "user_id": request.env.user.id,
        }
        if candidate:
            vals["candidate_id"] = candidate.id
        if data.get("location"):
            vals["location"] = data["location"]
        if partner_ids:
            vals["partner_ids"] = [(6, 0, partner_ids)]

        reminder_minutes = _coerce_int(data.get("reminderMinutes"), 15)
        if reminder_minutes and reminder_minutes > 0:
            alarm = _find_or_create_alarm(reminder_minutes)
            if alarm:
                vals["alarm_ids"] = [(6, 0, [alarm.id])]

        if existing_to_cancel:
            _cancel_event_and_notify(
                existing_to_cancel,
                reason=(data.get("replaceReason") or "rescheduled").strip(),
                comment=(data.get("replaceComment") or "").strip() or None,
                notify=_coerce_bool(data.get("notifyAttendees"), True),
            )

        event = request.env["calendar.event"].sudo().with_context(
            no_mail_to_attendees=True,
            mail_create_nolog=True,
        ).create(vals)

        if event.google_meet_url and not event.videocall_location:
            event.videocall_location = event.google_meet_url

        Attachment = request.env["ir.attachment"].sudo()
        event_attachments = Attachment

        if candidate:
            resume_att = _fetch_resume_attachment(candidate, event)
            if resume_att:
                event_attachments |= resume_att

        if job and job.description:
            html = (
                f"<html><body><h2>Job Description &mdash; {job.name}</h2>"
                f"{job.description}</body></html>"
            )
            event_attachments |= Attachment.create({
                "name": f"Job Description - {job.name}.html",
                "datas": base64.b64encode(html.encode("utf-8")),
                "res_model": "calendar.event",
                "res_id": event.id,
                "mimetype": "text/html",
            })

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

        if event.attendee_ids:
            _queue_invitation_emails(
                event.attendee_ids,
                extra_attachment_ids=event_attachments.ids if event_attachments else None,
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
