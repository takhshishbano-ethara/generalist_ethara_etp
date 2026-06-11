"""REST API — IRIS take-home / FSD assessments.

Assessment creation (candidate must be past screening — the model
constraint enforces it), submission upload (PDF / docx / md / txt), the
LLM review-draft trigger (touches NEITHER the assessment status NOR the
candidate state), structured feedback editing and human finalization.
Every trigger calls the SAME ``action_*`` model methods as the UI buttons.
"""

import base64
import binascii
import logging

from odoo import http
from odoo.http import request

from .common import (
    ASSESSMENT_RATINGS,
    ASSESSMENT_RECOMMENDATIONS,
    ASSESSMENT_STATUSES,
    _assessment_dict,
    _require_iris_manager,
    _require_iris_user,
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
BASE = "/api/v1/iris/assessments"

#: Plain text feedback fields writable through PUT /assessments/<id>/feedback.
_FEEDBACK_TEXT_FIELDS = (
    "summary",
    "strengths",
    "concerns",
    "fit_for_current_need",
    "recommendation_conditions",
)


def _assessment_or_404(aid):
    """Browse an assessment by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.assessment"].sudo().browse(aid).exists()
    if not rec:
        return None, return_Response(
            message="Assessment not found.",
            status=404,
            errors=["Assessment not found."],
        )
    return rec, None


class IrisAssessmentApi(http.Controller):
    """``/api/v1/iris/assessments`` endpoints."""

    # ------------------------------------------------------------------
    # Creation (nested under the candidate)
    # ------------------------------------------------------------------
    @http.route(
        CANDIDATES_BASE + "/<int:cid>/assessments",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_assessment_create(self, cid, **kwargs):
        """Create an assessment for a candidate (+ optional ``brief``).

        The model constraint rejects candidates that are not past
        screening (shipped / interview_ready / interviewed / scored).
        """
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
        data, err = read_json_body()
        if err is not None:
            return err

        vals = {"candidate_id": candidate.id}
        if data.get("brief"):
            vals["brief"] = data["brief"]
        rec = request.env["iris.assessment"].sudo().create(vals)
        return return_Response(
            message="Assessment created.",
            status=200,
            data={"assessment": _assessment_dict(rec, full=True)},
        )

    # ------------------------------------------------------------------
    # Collection / item
    # ------------------------------------------------------------------
    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_assessments_list(self, **kwargs):
        """List assessments; ``candidate_id`` / ``status`` filters + pagination."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        raw_cid = params.get("candidate_id")
        if raw_cid not in (None, ""):
            candidate_id = coerce_int(raw_cid, None)
            if candidate_id is None:
                msg = f"Invalid candidate_id '{raw_cid}'."
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("candidate_id", "=", candidate_id))
        status = (params.get("status") or "").strip()
        if status:
            if status not in ASSESSMENT_STATUSES:
                msg = (
                    f"Invalid status '{status}'. "
                    f"Allowed: {', '.join(ASSESSMENT_STATUSES)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("status", "=", status))

        Assessment = request.env["iris.assessment"].sudo()
        total = Assessment.search_count(domain)
        records = Assessment.search(
            domain, offset=offset, limit=limit, order="id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "assessments": [_assessment_dict(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(
        BASE + "/<int:aid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_detail(self, aid, **kwargs):
        """Full assessment detail incl. texts + LLM metadata."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"assessment": _assessment_dict(rec, full=True)},
        )

    @http.route(
        BASE + "/<int:aid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_delete(self, aid, **kwargs):
        """Delete an assessment (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Assessment deleted.", status=200)

    # ------------------------------------------------------------------
    # Submission + review pipeline
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:aid>/submission",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_submission(self, aid, **kwargs):
        """Upload the candidate's submission (PDF / docx / md / txt).

        The model extracts the text and moves draft/sent → submitted;
        unsupported formats surface as a clean 400.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        b64 = data.get("submission_base64")
        filename = (data.get("submission_filename") or "").strip()
        if not b64 or not filename:
            msg = "submission_base64 and submission_filename are required."
            return return_Response(message=msg, status=400, errors=[msg])
        try:
            base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            msg = "submission_base64 is not valid base64."
            return return_Response(message=msg, status=400, errors=[msg])

        rec.write({
            "submission_file": b64,
            "submission_filename": filename,
        })
        return return_Response(
            message="Submission uploaded.",
            status=200,
            data={"assessment": _assessment_dict(rec, full=True)},
        )

    @http.route(
        BASE + "/<int:aid>/review-draft",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_review_draft(self, aid, **kwargs):
        """Queue the LLM review draft (submitted assessments only).

        The draft touches NEITHER the assessment status NOR the candidate
        state — poll ``GET /assessments/<id>`` for ``llm_status``.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        rec.action_generate_review_draft()
        return return_Response(
            message="Review draft queued.",
            status=200,
            data={
                "assessment_id": rec.id,
                "status": rec.status,
                "llm_status": rec.llm_status,
            },
        )

    @http.route(
        BASE + "/<int:aid>/feedback",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_feedback(self, aid, **kwargs):
        """Write the structured feedback fields (locked once reviewed)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        if rec.status == "reviewed":
            msg = "The feedback is locked — this assessment is already reviewed."
            return return_Response(message=msg, status=400, errors=[msg])
        data, err = read_json_body()
        if err is not None:
            return err

        vals = {}
        if "rating" in data:
            rating = (data.get("rating") or "").strip().lower()
            if rating and rating not in ASSESSMENT_RATINGS:
                msg = (
                    f"Invalid rating '{rating}'. "
                    f"Allowed: {', '.join(ASSESSMENT_RATINGS)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            vals["rating"] = rating or False
        if "recommendation" in data:
            recommendation = (data.get("recommendation") or "").strip().lower()
            if recommendation and recommendation not in ASSESSMENT_RECOMMENDATIONS:
                msg = (
                    f"Invalid recommendation '{recommendation}'. "
                    f"Allowed: {', '.join(ASSESSMENT_RECOMMENDATIONS)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            vals["recommendation"] = recommendation or False
        for field in _FEEDBACK_TEXT_FIELDS:
            if field in data:
                vals[field] = data[field] or False

        if not vals:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])
        rec.write(vals)
        return return_Response(
            message="Feedback updated.",
            status=200,
            data={"assessment": _assessment_dict(rec, full=True)},
        )

    @http.route(
        BASE + "/<int:aid>/finalize",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_assessment_finalize(self, aid, **kwargs):
        """Human finalization → reviewed + Feedback.md attachment."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _assessment_or_404(aid)
        if err is not None:
            return err
        rec.action_finalize_review()
        return return_Response(
            message="Assessment review finalized.",
            status=200,
            data={"assessment": _assessment_dict(rec, full=True)},
        )
