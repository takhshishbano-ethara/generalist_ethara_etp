"""REST API — IRIS interviews + scorecards.

Interview-guide generation (candidate must be shipped), interviewer notes
submission, scorecard generation and read access to both artifact types.
All triggers call the SAME ``action_*`` model methods as the UI buttons.
"""

import logging
from datetime import datetime, timezone

from odoo import http
from odoo.http import request

from .common import (
    _interview_dict,
    _require_iris_manager,
    _require_iris_user,
    _scorecard_dict,
    coerce_int,
    handle_api_errors,
    paginate,
    pagination_block,
    read_json_body,
)
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

CANDIDATES_BASE = "/api/v1/iris/candidates"
INTERVIEWS_BASE = "/api/v1/iris/interviews"
SCORECARDS_BASE = "/api/v1/iris/scorecards"


def _interview_or_404(iid):
    """Browse an interview by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.interview"].sudo().browse(iid).exists()
    if not rec:
        return None, return_Response(
            message="Interview not found.",
            status=404,
            errors=["Interview not found."],
        )
    return rec, None


def _scorecard_or_404(scid):
    """Browse a scorecard by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.scorecard"].sudo().browse(scid).exists()
    if not rec:
        return None, return_Response(
            message="Scorecard not found.",
            status=404,
            errors=["Scorecard not found."],
        )
    return rec, None


def _parse_iso_datetime(raw, label):
    """ISO-8601 string → naive UTC datetime, or ``(None, 400 response)``."""
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        msg = f"Invalid {label} '{raw}'. Expected an ISO 8601 datetime."
        return None, return_Response(message=msg, status=400, errors=[msg])
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value, None


def _id_filter(params, key, domain_field, domain):
    """Append an int equality filter to ``domain``; 400 on a bad value."""
    raw = params.get(key)
    if raw in (None, ""):
        return None
    value = coerce_int(raw, None)
    if value is None:
        msg = f"Invalid {key} '{raw}'."
        return return_Response(message=msg, status=400, errors=[msg])
    domain.append((domain_field, "=", value))
    return None


def _latest(records):
    """Highest-id record of a recordset (empty recordset when none)."""
    return records.sorted("id", reverse=True)[:1]


class IrisInterviewApi(http.Controller):
    """``/api/v1/iris/interviews`` + ``/api/v1/iris/scorecards`` endpoints."""

    # ------------------------------------------------------------------
    # Interview creation (candidate-scoped)
    # ------------------------------------------------------------------
    @http.route(
        CANDIDATES_BASE + "/<int:cid>/interviews",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_interview_create(self, cid, **kwargs):
        """Create an interview + queue guide generation (state: shipped)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        candidate = request.env["iris.candidate"].sudo().browse(cid).exists()
        if not candidate:
            return return_Response(
                message="Candidate not found.",
                status=404,
                errors=["Candidate not found."],
            )
        if candidate.state != "shipped":
            msg = (
                "Interview guide can only be generated when the candidate "
                "is shipped."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        candidate.action_generate_guide()
        interview = _latest(candidate.interview_ids)
        return return_Response(
            message="Interview guide queued.",
            status=200,
            data={
                "interview_id": interview.id if interview else None,
                "llm_status": interview.llm_status if interview else None,
            },
        )

    # ------------------------------------------------------------------
    # Interviews
    # ------------------------------------------------------------------
    @http.route(
        INTERVIEWS_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_interviews_list(self, **kwargs):
        """List interviews; optional ``candidate_id`` filter + pagination."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        err = _id_filter(params, "candidate_id", "candidate_id", domain)
        if err is not None:
            return err

        Interview = request.env["iris.interview"].sudo()
        total = Interview.search_count(domain)
        records = Interview.search(
            domain, offset=offset, limit=limit, order="id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "interviews": [_interview_dict(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(
        INTERVIEWS_BASE + "/<int:iid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_interview_detail(self, iid, **kwargs):
        """Full interview: guide markdown, notes, LLM metadata."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _interview_or_404(iid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"interview": _interview_dict(rec, full=True)},
        )

    @http.route(
        INTERVIEWS_BASE + "/<int:iid>/notes",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_interview_notes(self, iid, **kwargs):
        """Record interviewer notes → candidate moves to interviewed."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _interview_or_404(iid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        notes = (data.get("notes") or "").strip()
        if not notes:
            msg = "notes is required."
            return return_Response(message=msg, status=400, errors=[msg])

        vals = {"notes": notes}
        if data.get("interviewer_name"):
            vals["interviewer_name"] = data["interviewer_name"]
        if data.get("interviewed_at"):
            interviewed_at, err = _parse_iso_datetime(
                data["interviewed_at"], "interviewed_at",
            )
            if err is not None:
                return err
            vals["interviewed_at"] = interviewed_at

        rec.write(vals)
        rec.action_submit_notes()
        return return_Response(
            message="Notes submitted.",
            status=200,
            data={
                "interview": _interview_dict(rec, full=True),
                "candidate_state": rec.candidate_id.state,
            },
        )

    @http.route(
        INTERVIEWS_BASE + "/<int:iid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_interview_delete(self, iid, **kwargs):
        """Delete an interview (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _interview_or_404(iid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Interview deleted.", status=200)

    # ------------------------------------------------------------------
    # Scorecards
    # ------------------------------------------------------------------
    @http.route(
        INTERVIEWS_BASE + "/<int:iid>/scorecard",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_interview_scorecard_create(self, iid, **kwargs):
        """Queue scorecard generation (model raises 400 when notes empty)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _interview_or_404(iid)
        if err is not None:
            return err
        rec.action_generate_scorecard()
        scorecard = _latest(rec.scorecard_ids)
        return return_Response(
            message="Scorecard queued.",
            status=200,
            data={
                "scorecard_id": scorecard.id if scorecard else None,
                "llm_status": scorecard.llm_status if scorecard else None,
            },
        )

    @http.route(
        SCORECARDS_BASE,
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_scorecards_list(self, **kwargs):
        """List scorecards; ``candidate_id``/``interview_id`` filters."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        for key, field in (
            ("candidate_id", "candidate_id"),
            ("interview_id", "interview_id"),
        ):
            err = _id_filter(params, key, field, domain)
            if err is not None:
                return err

        Scorecard = request.env["iris.scorecard"].sudo()
        total = Scorecard.search_count(domain)
        records = Scorecard.search(
            domain, offset=offset, limit=limit, order="id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "scorecards": [_scorecard_dict(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(
        SCORECARDS_BASE + "/<int:scid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_scorecard_detail(self, scid, **kwargs):
        """Full scorecard: markdown, notes snapshot, LLM metadata."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _scorecard_or_404(scid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"scorecard": _scorecard_dict(rec, full=True)},
        )

    @http.route(
        SCORECARDS_BASE + "/<int:scid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_scorecard_delete(self, scid, **kwargs):
        """Delete a scorecard (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _scorecard_or_404(scid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Scorecard deleted.", status=200)
