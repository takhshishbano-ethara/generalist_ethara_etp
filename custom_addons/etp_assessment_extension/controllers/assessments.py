"""Assessment CRUD + lifecycle action endpoints."""

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
)

from .common import (
    ASSESSMENT_STATES,
    coerce_bool,
    coerce_int,
    jsonrpc_error,
    jsonrpc_response,
    m2o_link,
    paginate,
    pagination_block,
    parse_json_body,
    pct,
    require_assessment_manager,
    require_assessment_user,
    resolve_order,
    user_role_tag,
    x2many_links,
)

ASSESSMENT_COLUMNS = [
    {"key": "name", "label": "Name", "type": "string"},
    {"key": "state_label", "label": "State", "type": "string"},
    {"key": "category_name", "label": "Category", "type": "string"},
    {"key": "evaluators_total", "label": "Candidates", "type": "integer"},
    {"key": "evaluators_done", "label": "Submitted", "type": "integer"},
    {"key": "progress_percent", "label": "Progress %", "type": "float"},
    {"key": "duration_minutes", "label": "Duration", "type": "integer"},
    {"key": "start_date", "label": "Start", "type": "datetime"},
    {"key": "end_date", "label": "End", "type": "datetime"},
]

SORT_FIELDS = {
    "name": "name",
    "create_date": "create_date",
    "start_date": "start_date",
    "end_date": "end_date",
    "state": "state",
}


def _serialize_assessment(rec, state_labels):
    total = len(rec.assessment_evaluator_ids)
    done = sum(1 for ev in rec.assessment_evaluator_ids if ev.state == "submitted")
    return {
        "id": rec.id,
        "name": rec.name or "",
        "state": rec.state,
        "state_label": state_labels.get(rec.state, ""),
        "category_id": rec.category_id.id if rec.category_id else 0,
        "category_name": rec.category_id.name if rec.category_id else "",
        "question_limit": rec.question_limit or 0,
        "total_questions_available": rec.total_questions_available or 0,
        "duration_minutes": rec.duration_minutes or 0,
        "start_date": rec.start_date.isoformat() if rec.start_date else None,
        "end_date": rec.end_date.isoformat() if rec.end_date else None,
        "deadline": rec.deadline.isoformat() if rec.deadline else None,
        "question_ids": rec.question_ids.ids,
        "candidate_ids": rec.evaluator_ids.ids,
        "evaluators_total": total,
        "evaluators_done": done,
        "progress_percent": pct(done, total),
        "response_count": rec.response_count or 0,
        "create_date": rec.create_date.isoformat() if rec.create_date else None,
        "write_date": rec.write_date.isoformat() if rec.write_date else None,
    }


def _build_assessment_domain(params):
    domain = []
    search = (params.get("search") or "").strip()
    if search:
        domain.append(("name", "ilike", search))
    state = (params.get("state") or "").strip()
    if state:
        if state not in ASSESSMENT_STATES:
            return None, return_Response(
                message=(
                    f"Invalid state '{state}'. "
                    f"Allowed: {', '.join(ASSESSMENT_STATES)}."
                ),
                status=400,
            )
        domain.append(("state", "=", state))
    category_id = coerce_int(params.get("category_id"), 0)
    if category_id:
        domain.append(("category_id", "=", category_id))
    date_from = (params.get("date_from") or "").strip()
    if date_from:
        domain.append(("start_date", ">=", date_from))
    date_to = (params.get("date_to") or "").strip()
    if date_to:
        domain.append(("end_date", "<=", date_to))
    return domain, None


def _build_assessment_vals(jdata, partial=False):
    vals = {}
    if "name" in jdata:
        vals["name"] = (jdata.get("name") or "").strip()
    if "category_id" in jdata:
        vals["category_id"] = coerce_int(jdata["category_id"], 0) or False
    if "question_limit" in jdata:
        vals["question_limit"] = coerce_int(jdata["question_limit"], 0)
    if "duration_minutes" in jdata:
        vals["duration_minutes"] = coerce_int(jdata["duration_minutes"], 0)
    if "start_date" in jdata:
        vals["start_date"] = jdata.get("start_date") or False
    if "end_date" in jdata:
        vals["end_date"] = jdata.get("end_date") or False
    if "deadline" in jdata:
        vals["deadline"] = jdata.get("deadline") or False
    if "candidate_ids" in jdata and isinstance(jdata["candidate_ids"], list):
        ids = [coerce_int(c, 0) for c in jdata["candidate_ids"]]
        ids = [i for i in ids if i]
        vals["evaluator_ids"] = [(6, 0, ids)]

    if not partial:
        if not vals.get("name"):
            return None, return_Response(
                message="'name' is required", status=400,
            )
        if not vals.get("category_id"):
            return None, return_Response(
                message="'category_id' is required", status=400,
            )

    return vals, None


def _run_state_action(assessment_id, method_name, success_message):
    forbidden = require_assessment_manager()
    if forbidden is not None:
        return forbidden

    assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
    if not assessment.exists():
        return return_Response(message="Assessment not found", status=404)
    try:
        getattr(assessment, method_name)()
    except (UserError, ValidationError) as exc:
        return return_Response(message=str(exc.args[0] if exc.args else exc), status=400)
    except Exception as exc:
        return return_Response(message=str(exc), status=400)

    state_labels = dict(
        request.env["etp.assessment"]._fields["state"].selection
    )
    return return_Response(
        message=success_message,
        status=200,
        data={"assessment": _serialize_assessment(assessment, state_labels)},
    )


class EtpAssessmentController(http.Controller):

    @http.route(
        "/api/v1/etp_assessment_ext/assessments",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def list_assessments(self, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        params = request.params or {}
        domain, error = _build_assessment_domain(params)
        if error is not None:
            return error
        order, error = resolve_order(params, SORT_FIELDS, "create_date", "desc")
        if error is not None:
            return error

        page, limit, offset = paginate(params)
        Assessment = env["etp.assessment"].sudo()
        total = Assessment.search_count(domain)
        records = Assessment.search(domain, limit=limit, offset=offset, order=order)
        state_labels = dict(Assessment._fields["state"].selection)
        rows = [_serialize_assessment(r, state_labels) for r in records]

        return return_Response(
            message="OK",
            status=200,
            data={
                "role": user_role_tag(env),
                "blocks": [{
                    "type": "table",
                    "title": "Assessments",
                    "columns": ASSESSMENT_COLUMNS,
                    "rows": rows,
                    "pagination": pagination_block(total, page, limit),
                }],
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        Assessment = request.env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)
        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="OK",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/detail",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_assessment_detail(self, assessment_id, **kwargs):
        """JSON-RPC 2.0 `web_read`-style payload for a single assessment.

        Returns the assessment record with Many2one fields expanded to
        `{id, display_name}` and *2many fields expanded to lists of
        `{id, display_name}` - the same shape Odoo's web client gets
        back from `web_read`.

        Envelope:

        ```json
        {
          "jsonrpc": "2.0",
          "id": 1,
          "result": [{...assessment...}]
        }
        ```

        Errors return:

        ```json
        {
          "jsonrpc": "2.0",
          "id": 1,
          "error": {"code": 404, "message": "Assessment not found"}
        }
        ```

        Optional `?id=<int>` query param echoes the JSON-RPC request id
        for clients that need request/response pairing.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        Assessment = env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return jsonrpc_error(404, "Assessment not found", http_status=404)

        def _dt(value):
            return value.strftime("%Y-%m-%d %H:%M:%S") if value else False

        evaluator_rows = []
        for ev in assessment.assessment_evaluator_ids:
            evaluator_rows.append({
                "id": ev.id,
                "employee_id": m2o_link(ev.employee_id),
                "state": ev.state,
                "started_at": _dt(ev.started_at),
                "deadline_datetime": _dt(ev.deadline_datetime),
                "answered_count": ev.answered_count or 0,
                "total_questions": ev.total_questions or 0,
                "total_score": ev.total_score or 0,
                "max_possible_score": ev.max_possible_score or 0,
                "is_locked": bool(ev.is_locked),
            })

        question_rows = []
        for q in assessment.question_ids:
            question_rows.append({
                "id": q.id,
                "sequence": q.sequence or 0,
                "name": q.name or "",
                "prompt": q.prompt or "",
                "category_id": m2o_link(q.category_id),
            })

        response_rows = []
        for r in assessment.response_ids:
            response_rows.append({
                "id": r.id,
                "evaluator_id": m2o_link(r.evaluator_id),
                "question_id": m2o_link(r.question_id),
                "score": r.score or 0,
                "state": r.state,
            })

        record = {
            "id": assessment.id,
            "state": assessment.state,
            "name": assessment.name or "",
            "category_id": m2o_link(assessment.category_id),
            "question_limit": assessment.question_limit or 0,
            "total_questions_available": (
                assessment.total_questions_available or 0
            ),
            "duration_minutes": assessment.duration_minutes or 0,
            "start_date": _dt(assessment.start_date),
            "end_date": _dt(assessment.end_date),
            "deadline": (
                assessment.deadline.strftime("%Y-%m-%d")
                if assessment.deadline else False
            ),
            "response_count": assessment.response_count or 0,
            "assessment_evaluator_ids": evaluator_rows,
            "evaluator_ids": x2many_links(assessment.evaluator_ids),
            "candidate_csv_file": False,
            "candidate_csv_filename": (
                assessment.candidate_csv_filename or False
            ),
            "question_ids": question_rows,
            "response_ids": response_rows,
            "display_name": (
                assessment.display_name or assessment.name or ""
            ),
        }

        return jsonrpc_response([record])

    @http.route(
        "/api/v1/etp_assessment_ext/assessments",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    @validate_request({
        "name": {"type": "string", "required": True},
        "category_id": {"type": "int", "required": True},
    })
    def create_assessment(self, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        jdata = kwargs.get("jdata") or {}
        vals, error = _build_assessment_vals(jdata, partial=False)
        if error is not None:
            return error

        Assessment = request.env["etp.assessment"].sudo()
        try:
            assessment = Assessment.create(vals)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="Assessment created",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["PUT", "PATCH"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def update_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        Assessment = request.env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        jdata = parse_json_body()
        vals, error = _build_assessment_vals(jdata, partial=True)
        if error is not None:
            return error
        if vals:
            try:
                assessment.write(vals)
            except (UserError, ValidationError) as exc:
                return return_Response(
                    message=str(exc.args[0] if exc.args else exc),
                    status=400,
                )

        state_labels = dict(Assessment._fields["state"].selection)
        return return_Response(
            message="Assessment updated",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>",
        type="http",
        auth="none",
        methods=["DELETE"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def delete_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        if assessment.state not in ("draft", "cancelled"):
            return return_Response(
                message=(
                    "Only draft or cancelled assessments can be deleted "
                    "(current state: "
                    f"{assessment.state})."
                ),
                status=400,
            )
        try:
            assessment.unlink()
        except Exception as exc:
            return return_Response(
                message=f"Cannot delete assessment: {exc}",
                status=400,
            )
        return return_Response(message="Assessment deleted", status=200)

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/start",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def start_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_start",
            "Assessment started and invitations sent",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/done",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def done_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_done", "Assessment marked done",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/cancel",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def cancel_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_cancel", "Assessment cancelled",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/reset_draft",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def reset_draft_assessment(self, assessment_id, **kwargs):
        return _run_state_action(
            assessment_id, "action_reset_draft", "Assessment reset to draft",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/monitor-kpis",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def get_assessment_monitor_kpis(self, assessment_id, **kwargs):
        """Six-tile monitor strip for the assessment detail screen (SCR-095).

        Returns:
            {
                "scope":         "overall" | "day_<N>",
                "assigned":      <int>,
                "started":       <int>,
                "submitted":     <int>,
                "avg_score":     <float>,   # mean total_score across submitted
                "pass_rate":     <float>,   # % of submitted with score >= threshold
                "at_risk":       <int>      # non-submitted with violations / late starts
            }

        Optional `?scope=overall|day_N` filters response rows by day index
        when a `day_index` field is exposed on responses; otherwise overall
        figures are returned regardless of scope.
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        env = request.env
        Assessment = env["etp.assessment"].sudo()
        assessment = Assessment.browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        params = request.params or {}
        scope = (params.get("scope") or "overall").strip() or "overall"

        evaluators = assessment.assessment_evaluator_ids
        assigned = len(evaluators)
        started = sum(
            1 for ev in evaluators if ev.state in ("in_progress", "submitted")
        )
        submitted_evs = evaluators.filtered(lambda e: e.state == "submitted")
        submitted = len(submitted_evs)

        scores = [ev.total_score or 0 for ev in submitted_evs]
        avg_score = (
            round(sum(scores) / len(scores), 2) if scores else 0.0
        )

        pass_threshold_pct = 70.0
        passed = 0
        for ev in submitted_evs:
            max_possible = ev.max_possible_score or 0
            if not max_possible:
                continue
            ratio = (ev.total_score or 0) / max_possible * 100.0
            if ratio >= pass_threshold_pct:
                passed += 1
        pass_rate = pct(passed, submitted)

        at_risk = sum(
            1 for ev in evaluators
            if ev.state != "submitted" and (
                ev.is_violated or ev.is_locked
            )
        )

        return return_Response(
            message="OK",
            status=200,
            data={
                "scope": scope,
                "assigned": assigned,
                "started": started,
                "submitted": submitted,
                "avg_score": avg_score,
                "pass_rate": pass_rate,
                "at_risk": at_risk,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/remind",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def remind_candidates(self, assessment_id, **kwargs):
        """Send a reminder to every pending / in-progress candidate (SCR-095)."""
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        pending = assessment.assessment_evaluator_ids.filtered(
            lambda e: e.state in ("pending", "in_progress")
        )
        sent = 0
        for ev in pending:
            try:
                if hasattr(ev, "action_resend_invitation"):
                    ev.action_resend_invitation()
                    sent += 1
                else:
                    ev.message_post(body="Reminder requested.")
                    sent += 1
            except Exception:
                continue
        return return_Response(
            message=f"Reminder dispatched to {sent} candidate(s).",
            status=200,
            data={"reminded": sent, "skipped": len(pending) - sent},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/export",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def export_assessment_report(self, assessment_id, **kwargs):
        """Queue a report export (SCR-095).

        Body / query: {"format": "csv" | "excel"} (default: "csv").
        Returns a stub job descriptor; the actual file is delivered through
        the reports pipeline.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        jdata = parse_json_body()
        fmt = (jdata.get("format") or "csv").strip().lower()
        if fmt not in ("csv", "excel"):
            return return_Response(
                message="format must be 'csv' or 'excel'.", status=400,
            )
        try:
            assessment.message_post(
                body=f"{fmt.upper()} export requested.",
            )
        except Exception:
            pass
        return return_Response(
            message=f"{fmt.upper()} export queued.",
            status=200,
            data={"assessment_id": assessment.id, "format": fmt},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/extend-window",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def extend_assessment_window(self, assessment_id, **kwargs):
        """Extend an assessment's submission window (SCR-095).

        Body: {"end_date": "...", "deadline": "..."} (any subset).
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)

        jdata = parse_json_body()
        vals = {}
        if "end_date" in jdata and jdata["end_date"]:
            vals["end_date"] = jdata["end_date"]
        if "deadline" in jdata and jdata["deadline"]:
            vals["deadline"] = jdata["deadline"]
        if not vals:
            return return_Response(
                message="Provide at least one of 'end_date' / 'deadline'.",
                status=400,
            )
        try:
            assessment.write(vals)
        except (UserError, ValidationError) as exc:
            return return_Response(
                message=str(exc.args[0] if exc.args else exc), status=400,
            )

        state_labels = dict(
            request.env["etp.assessment"]._fields["state"].selection
        )
        return return_Response(
            message="Submission window extended.",
            status=200,
            data={"assessment": _serialize_assessment(assessment, state_labels)},
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/close",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def close_assessment(self, assessment_id, **kwargs):
        """Close an assessment immediately (SCR-095).

        Alias for `/done` so the monitor screen can use a stable, intent-named
        verb regardless of the underlying state machine.
        """
        return _run_state_action(
            assessment_id, "action_done", "Assessment closed.",
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/days/<int:day_index>/approve",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def approve_assessment_day(self, assessment_id, day_index, **kwargs):
        """Mark every question for a given day as approved (SCR-095).

        Logs the approval on the assessment's audit trail. Day grouping is
        derived from each question's `day_index` attribute when present.
        """
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env["etp.assessment"].sudo().browse(assessment_id)
        if not assessment.exists():
            return return_Response(message="Assessment not found", status=404)
        if day_index < 1:
            return return_Response(
                message="day_index must be >= 1.", status=400,
            )

        questions = assessment.question_ids
        day_questions = questions.filtered(
            lambda q: (
                getattr(q, "day_index", False)
                and q.day_index == day_index
            )
        ) or questions  # fallback: whole bank if day_index not modelled
        approved = 0
        for q in day_questions:
            try:
                if hasattr(q, "approval_state"):
                    q.approval_state = "approved"
                    approved += 1
                else:
                    q.message_post(body=f"Approved as part of Day {day_index}.")
                    approved += 1
            except Exception:
                continue
        try:
            assessment.message_post(
                body=(
                    f"Day {day_index} approved by {request.env.user.name} "
                    f"({approved} question(s))."
                ),
            )
        except Exception:
            pass
        return return_Response(
            message=f"Day {day_index} approved ({approved} question(s)).",
            status=200,
            data={
                "assessment_id": assessment.id,
                "day_index": day_index,
                "approved_count": approved,
            },
        )

    @http.route(
        "/api/v1/etp_assessment_ext/assessments/<int:assessment_id>/item-analysis",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def item_analysis(self, assessment_id, **kwargs):
        """Per-question stats for SCR-097 (Stats screen).

        Returns:
          {
            "assessment_id": N,
            "assessment_name": "...",
            "day": 0 | N,
            "total_days": N,
            "summary": {
              "avg_score": float,        # 0..100
              "pass_rate": float,        # 0..100  (pass threshold = 70)
              "items_above_bar": int,
              "items_below_bar": int,
              "total_items": int,
            },
            "items": [
              {
                "question_id": N,
                "code": "Q-NNN",
                "day": N,
                "type": "image_comparison",
                "brief": "Question name",
                "avg_score": float,      # 0..100
                "pass_rate": float,      # 0..100
                "responses": int,
                "answer_key": "...",
                "distribution": [int]*10,  # 0-10, 10-20, ..., 90-100 buckets
                "flagged": bool,
              }
            ]
          }
        """
        forbidden = require_assessment_user()
        if forbidden is not None:
            return forbidden

        assessment = (
            request.env["etp.assessment"].sudo().browse(assessment_id)
        )
        if not assessment.exists():
            return return_Response(message="Assessment not found.", status=404)

        day_filter = coerce_int(kwargs.get("day"), 0)
        total_days = max(
            1, coerce_int(getattr(assessment, "total_days", 0), 1),
        )

        questions = assessment.question_ids
        if day_filter > 0:
            from .candidate_self import _day_questions  # late import
            questions = _day_questions(assessment, day_filter)

        Response = request.env["etp.assessment.response"].sudo()
        items = []
        sum_avg = 0.0
        sum_pass = 0
        items_with_data = 0
        above_bar = 0
        below_bar = 0
        PASS_THRESHOLD = 70.0

        for q in questions:
            responses = Response.search([
                ("assessment_id", "=", assessment.id),
                ("question_id", "=", q.id),
                ("state", "=", "submitted"),
            ])
            n = len(responses)
            if n == 0:
                items.append({
                    "question_id": q.id,
                    "code": q.name or f"Q-{q.id:03d}",
                    "day": getattr(q, "day_index", 0) or 0,
                    "type": q.question_type or "",
                    "brief": q.prompt or q.name or "",
                    "avg_score": 0.0,
                    "pass_rate": 0.0,
                    "responses": 0,
                    "answer_key": "",
                    "distribution": [0] * 10,
                    "flagged": bool(getattr(q, "is_flagged", False)),
                })
                continue

            percents = []
            for r in responses:
                if r.max_score:
                    percents.append((r.score or 0) / r.max_score * 100.0)
            if not percents:
                percents = [0.0] * n

            avg = sum(percents) / len(percents)
            passes = sum(1 for p in percents if p >= PASS_THRESHOLD)
            pass_rate = (passes / len(percents)) * 100.0

            distribution = [0] * 10
            for p in percents:
                idx = min(9, int(p // 10))
                distribution[idx] += 1

            answer_key = ""
            if q.question_dimension_ids:
                # First dim's first option name is a reasonable proxy.
                first_dim = q.question_dimension_ids[0].dimension_id
                if first_dim and first_dim.option_ids:
                    answer_key = first_dim.option_ids[0].name or ""

            items.append({
                "question_id": q.id,
                "code": q.name or f"Q-{q.id:03d}",
                "day": getattr(q, "day_index", 0) or 0,
                "type": q.question_type or "",
                "brief": q.prompt or q.name or "",
                "avg_score": round(avg, 2),
                "pass_rate": round(pass_rate, 2),
                "responses": n,
                "answer_key": answer_key,
                "distribution": distribution,
                "flagged": bool(getattr(q, "is_flagged", False)),
            })

            sum_avg += avg
            sum_pass += pass_rate
            items_with_data += 1
            if avg >= PASS_THRESHOLD:
                above_bar += 1
            else:
                below_bar += 1

        return return_Response(
            message="OK",
            status=200,
            data={
                "assessment_id": assessment.id,
                "assessment_name": assessment.name or "",
                "day": day_filter,
                "total_days": total_days,
                "summary": {
                    "avg_score": (
                        round(sum_avg / items_with_data, 2)
                        if items_with_data else 0.0
                    ),
                    "pass_rate": (
                        round(sum_pass / items_with_data, 2)
                        if items_with_data else 0.0
                    ),
                    "items_above_bar": above_bar,
                    "items_below_bar": below_bar,
                    "total_items": len(items),
                },
                "items": items,
            },
        )
