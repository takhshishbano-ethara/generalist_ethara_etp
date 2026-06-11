"""REST API — IRIS candidates.

Full candidate CRUD plus pipeline triggers (screen / evidence / re-screen /
manual verdict / final decision / BLOCK sign-off / clarifying questions /
status polling) and resume upload + presigned download. Every route is
token-authenticated via ``api_auth_gateway`` and calls the SAME ``action_*``
model methods as the Odoo UI buttons — single code path for all state
transitions.
"""

import base64
import binascii
import logging

from odoo import fields, http
from odoo.http import request

from .common import (
    BLOCK_KINDS,
    CANDIDATE_STATES,
    FINAL_DECISIONS,
    RESCREEN_REASONS,
    SCREENING_VERDICTS,
    _candidate_detail,
    _candidate_summary,
    _iso,
    _require_iris_manager,
    _require_iris_user,
    _resolve_role,
    _screening_dict,
    coerce_bool,
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

BASE = "/api/v1/iris/candidates"

# Plain fields writable through POST /candidates and PUT /candidates/<id>
# (the role is resolved separately via _resolve_role; jd_id is validated).
_WRITABLE_FIELDS = ("name", "email", "phone", "tech_date_reference")
# Fields locked once the candidate has left draft / needs_review.
_LOCKED_PAST_DRAFT = (
    "role_id",
    "role_code",
    "target_role",
    "jd_id",
    "resume_base64",
    "resume_filename",
)


def _candidate_or_404(cid):
    """Browse a candidate by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.candidate"].sudo().browse(cid).exists()
    if not rec:
        return None, return_Response(
            message="Candidate not found.",
            status=404,
            errors=["Candidate not found."],
        )
    return rec, None


def _resume_vals(data):
    """Validate the resume payload → ``(vals, None)`` or ``(None, 400)``.

    Requires both ``resume_base64`` and ``resume_filename``; the base64 is
    decoded once here for a clear early error (the model re-validates the
    %PDF magic bytes and raises UserError → 400).
    """
    b64 = data.get("resume_base64")
    filename = (data.get("resume_filename") or "").strip()
    if not b64 or not filename:
        msg = "resume_base64 and resume_filename are required."
        return None, return_Response(message=msg, status=400, errors=[msg])
    try:
        base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        msg = "resume_base64 is not valid base64."
        return None, return_Response(message=msg, status=400, errors=[msg])
    return {"resume_file": b64, "resume_filename": filename}, None


def _latest(records):
    """Highest-id record of a recordset (empty recordset when none)."""
    return records.sorted("id", reverse=True)[:1]


def _jd_vals(data):
    """Validate the optional ``jd_id`` key → ``(vals, None)`` or ``(None, 400)``.

    Only an APPROVED ``iris.job.description`` may be linked; ``null`` clears
    the link. Returns ``({}, None)`` when the key is absent.
    """
    if "jd_id" not in data:
        return {}, None
    raw = data.get("jd_id")
    if raw in (None, False, ""):
        return {"jd_id": False}, None
    jd_id = coerce_int(raw, None)
    jd = (
        request.env["iris.job.description"].sudo().browse(jd_id).exists()
        if jd_id
        else None
    )
    if not jd:
        msg = f"jd_id '{raw}' not found."
        return None, return_Response(message=msg, status=400, errors=[msg])
    if jd.state != "approved":
        msg = (
            "Only an approved job description can be linked to a candidate "
            f"(JD {jd.id} is {jd.state})."
        )
        return None, return_Response(message=msg, status=400, errors=[msg])
    return {"jd_id": jd.id}, None


def _batch_vals(data, role):
    """Validate the optional ``batch_id`` key on candidate creation.

    The batch must exist, still be in Draft, and share the candidate's
    role. Returns ``({}, None)`` when the key is absent.
    """
    raw = data.get("batch_id")
    if raw in (None, False, ""):
        return {}, None
    batch_id = coerce_int(raw, None)
    batch = (
        request.env["iris.screening.batch"].sudo().browse(batch_id).exists()
        if batch_id
        else None
    )
    if not batch:
        msg = f"batch_id '{raw}' not found."
        return None, return_Response(message=msg, status=400, errors=[msg])
    if batch.state != "draft":
        msg = (
            f"Members can only be added while the batch is in Draft "
            f"(batch {batch.name} is {batch.state})."
        )
        return None, return_Response(message=msg, status=400, errors=[msg])
    if role and batch.role_id != role:
        msg = (
            f"The candidate role must match the batch role "
            f"({batch.role_id.name})."
        )
        return None, return_Response(message=msg, status=400, errors=[msg])
    return {"batch_id": batch.id}, None


class IrisCandidateApi(http.Controller):
    """``/api/v1/iris/candidates`` endpoints."""

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_candidates_list(self, **kwargs):
        """List candidates with ``page``/``limit``/``state``/``search``."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        state = (params.get("state") or "").strip()
        if state:
            if state not in CANDIDATE_STATES:
                msg = (
                    f"Invalid state '{state}'. "
                    f"Allowed: {', '.join(CANDIDATE_STATES)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("state", "=", state))
        search = (params.get("search") or "").strip()
        if search:
            domain += ["|", ("name", "ilike", search), ("email", "ilike", search)]

        Candidate = request.env["iris.candidate"].sudo()
        total = Candidate.search_count(domain)
        records = Candidate.search(
            domain, offset=offset, limit=limit, order="create_date desc, id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "candidates": [_candidate_summary(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(BASE, type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False)
    @validate_token
    @handle_api_errors
    def iris_candidates_create(self, **kwargs):
        """Create a candidate; ``name`` + a role (id / code / name) required."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        data, err = read_json_body()
        if err is not None:
            return err

        name = (data.get("name") or "").strip()
        if not name:
            msg = "name is required."
            return return_Response(message=msg, status=400, errors=[msg])
        role, err = _resolve_role(data, required=True)
        if err is not None:
            return err

        vals = {"name": name, "role_id": role.id}
        for field in ("email", "phone", "tech_date_reference"):
            if field in data:
                vals[field] = data[field] or False

        jd_vals, err = _jd_vals(data)
        if err is not None:
            return err
        vals.update(jd_vals)

        batch_vals, err = _batch_vals(data, role)
        if err is not None:
            return err
        vals.update(batch_vals)

        if data.get("resume_base64") or data.get("resume_filename"):
            resume_vals, err = _resume_vals(data)
            if err is not None:
                return err
            vals.update(resume_vals)

        rec = request.env["iris.candidate"].sudo().create(vals)
        return return_Response(
            message="Candidate created.",
            status=200,
            data={"candidate": _candidate_detail(rec)},
        )

    # ------------------------------------------------------------------
    # Item
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:cid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_detail(self, cid, **kwargs):
        """Full candidate detail incl. nested artifact summaries."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"candidate": _candidate_detail(rec)},
        )

    @http.route(
        BASE + "/<int:cid>",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_update(self, cid, **kwargs):
        """Update whitelisted fields; role/resume locked past draft."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err
        if not data:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])

        if rec.state not in ("draft", "needs_review") and any(
            field in data for field in _LOCKED_PAST_DRAFT
        ):
            msg = (
                "Role, linked job description and resume can only be edited "
                "while the candidate is in draft or needs_review."
            )
            return return_Response(message=msg, status=400, errors=[msg])

        vals = {}
        for field in _WRITABLE_FIELDS:
            if field in data:
                vals[field] = data[field] or False
        if "name" in vals and not vals["name"]:
            msg = "name cannot be empty."
            return return_Response(message=msg, status=400, errors=[msg])

        role, err = _resolve_role(data)
        if err is not None:
            return err
        if role is not None:
            vals["role_id"] = role.id

        jd_vals, err = _jd_vals(data)
        if err is not None:
            return err
        vals.update(jd_vals)

        if data.get("resume_base64"):
            resume_vals, err = _resume_vals(data)
            if err is not None:
                return err
            vals.update(resume_vals)
        elif data.get("resume_filename"):
            msg = "resume_filename requires resume_base64."
            return return_Response(message=msg, status=400, errors=[msg])

        if not vals:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])

        rec.write(vals)
        return return_Response(
            message="Candidate updated.",
            status=200,
            data={"candidate": _candidate_detail(rec)},
        )

    @http.route(
        BASE + "/<int:cid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_delete(self, cid, **kwargs):
        """Delete a candidate (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Candidate deleted.", status=200)

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:cid>/resume",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_resume_upload(self, cid, **kwargs):
        """Upload/replace the resume PDF (draft or needs_review only).

        Same lock as PUT /candidates/<id>: a needs_review candidate may
        replace the resume before a manual re-screen.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        if rec.state not in ("draft", "needs_review"):
            msg = (
                "Resume can only be uploaded while the candidate is in "
                "draft or needs_review."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        data, err = read_json_body()
        if err is not None:
            return err
        resume_vals, err = _resume_vals(data)
        if err is not None:
            return err
        rec.write(resume_vals)
        return return_Response(
            message="Resume uploaded.",
            status=200,
            data={"candidate": _candidate_detail(rec)},
        )

    @http.route(
        BASE + "/<int:cid>/resume",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_resume_download(self, cid, **kwargs):
        """Time-limited presigned S3 GET URL for the stored resume."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        if not rec.resume_s3_key:
            msg = "No resume stored on S3 for this candidate."
            return return_Response(message=msg, status=404, errors=[msg])

        icp = request.env["ir.config_parameter"].sudo()
        connector_id = coerce_int(icp.get_param("iris.s3_connector_id"), 0)
        connector = (
            request.env["s3.connector"].sudo().browse(connector_id).exists()
            if connector_id
            else None
        )
        if not connector:
            msg = "No S3 connector configured for IRIS."
            return return_Response(message=msg, status=404, errors=[msg])

        params = request.params or {}
        expires_in = min(max(coerce_int(params.get("expires_in"), 300), 30), 3600)
        # A misconfigured connector / boto3 failure raises S3StorageError;
        # map it to a clean 400 instead of letting it fall through to the
        # generic 500 in handle_api_errors.
        from ..models.iris_s3_storage import S3StorageError
        try:
            url = request.env["iris.s3.storage"].presigned_get_url(
                connector.id,
                rec.resume_s3_key,
                expires_in=expires_in,
                mimetype="application/pdf",
                filename=rec.resume_filename or None,
            )
        except S3StorageError as exc:
            msg = "Could not generate a resume download URL: %s" % exc
            return return_Response(message=msg, status=400, errors=[str(exc)])
        return return_Response(
            message="OK",
            status=200,
            data={"url": url, "expires_in": expires_in},
        )

    # ------------------------------------------------------------------
    # Pipeline triggers
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:cid>/screen",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_screen(self, cid, **kwargs):
        """Trigger the LLM screening (async — poll the GETs for results)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        rec.action_screen()
        screening = _latest(rec.screening_ids)
        return return_Response(
            message="Screening queued.",
            status=200,
            data={
                "screening_id": screening.id if screening else None,
                "llm_status": screening.llm_status if screening else None,
            },
        )

    @http.route(
        BASE + "/<int:cid>/evidence",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_evidence(self, cid, **kwargs):
        """Record HOLD verification evidence (+ optional ``rescreen_now``).

        Same write path as the UI evidence wizard: the evidence lands on the
        current HOLD screening; ``rescreen_now`` immediately chains into
        ``action_rescreen``.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        evidence = (data.get("evidence") or "").strip()
        if not evidence:
            msg = "evidence is required."
            return return_Response(message=msg, status=400, errors=[msg])
        if rec.state != "hold":
            msg = "Evidence can only be recorded while the candidate is on HOLD."
            return return_Response(message=msg, status=400, errors=[msg])

        screening = rec.current_screening_id
        if not screening or screening.verdict != "hold":
            screening = _latest(
                rec.screening_ids.filtered(lambda s: s.verdict == "hold"),
            )
        if not screening:
            msg = "No HOLD screening found for this candidate."
            return return_Response(message=msg, status=400, errors=[msg])

        screening.write(
            {
                "verification_evidence": evidence,
                "evidence_recorded_by": request.env.uid,
                "evidence_recorded_at": fields.Datetime.now(),
            },
        )

        payload = {
            "screening": _screening_dict(screening, full=True),
            "candidate_state": rec.state,
        }
        if coerce_bool(data.get("rescreen_now"), False):
            rec.action_rescreen()
            new_screening = _latest(rec.screening_ids)
            payload.update(
                {
                    "candidate_state": rec.state,
                    "screening_id": new_screening.id if new_screening else None,
                    "llm_status": (
                        new_screening.llm_status if new_screening else None
                    ),
                },
            )
        return return_Response(message="Evidence recorded.", status=200, data=payload)

    @http.route(
        BASE + "/<int:cid>/rescreen",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_rescreen(self, cid, **kwargs):
        """Re-screen a candidate.

        Optional ``reason``: ``hold_evidence`` (default — HOLD candidates,
        400 if no evidence recorded) or ``batch_consistency`` (manager only
        — advisory re-screen from a completed batch consistency review).
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        reason = (data.get("reason") or "").strip().lower() or "hold_evidence"
        if reason not in RESCREEN_REASONS:
            msg = (
                f"Invalid reason '{reason}'. "
                f"Allowed: {', '.join(RESCREEN_REASONS)}."
            )
            return return_Response(message=msg, status=400, errors=[msg])

        if reason == "batch_consistency":
            guard = _require_iris_manager()
            if guard is not None:
                return guard
            rec.action_rescreen_advisory()
        else:
            rec.action_rescreen()
        screening = _latest(rec.screening_ids)
        return return_Response(
            message="Re-screening queued.",
            status=200,
            data={
                "screening_id": screening.id if screening else None,
                "llm_status": screening.llm_status if screening else None,
                "rescreen_reason": reason,
            },
        )

    @http.route(
        BASE + "/<int:cid>/verdict",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_manual_verdict(self, cid, **kwargs):
        """Manager manual verdict for a needs_review candidate."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err
        verdict = (data.get("verdict") or "").strip().lower()
        if verdict not in SCREENING_VERDICTS:
            msg = (
                f"Invalid verdict '{verdict}'. "
                f"Allowed: {', '.join(SCREENING_VERDICTS)}."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        getattr(rec, f"action_manual_verdict_{verdict}")()
        return return_Response(
            message="Verdict recorded.",
            status=200,
            data={"candidate": _candidate_summary(rec)},
        )

    # ------------------------------------------------------------------
    # Dual sign-off on BLOCK (v1.1)
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:cid>/block-signoff",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_block_signoff(self, cid, **kwargs):
        """Co-sign a pending BLOCK (any iris user EXCEPT the proposer).

        Body: ``block_kind`` (credibility / competence — selects the
        rejection letter) + optional ``note`` posted to the chatter.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        block_kind = (data.get("block_kind") or "").strip().lower()
        if block_kind not in BLOCK_KINDS:
            msg = (
                f"Invalid block_kind '{block_kind}'. "
                f"Allowed: {', '.join(BLOCK_KINDS)}."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        note = (data.get("note") or "").strip()

        rec._block_signoff(block_kind)
        if note:
            rec.message_post(
                body=f"BLOCK sign-off note from {request.env.user.name}: {note}",
            )
        screening = _latest(
            rec.screening_ids.filtered(lambda s: s.verdict == "block"),
        )
        return return_Response(
            message="BLOCK co-signed.",
            status=200,
            data={
                "candidate": _candidate_summary(rec),
                "screening": (
                    _screening_dict(screening, full=True) if screening else None
                ),
            },
        )

    @http.route(
        BASE + "/<int:cid>/block-signoff/reject",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_block_signoff_reject(self, cid, **kwargs):
        """Reject a pending BLOCK sign-off → Needs Review (reason required)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        reason = (data.get("reason") or "").strip()
        if not reason:
            msg = "reason is required."
            return return_Response(message=msg, status=400, errors=[msg])

        rec._block_signoff_reject(reason)
        screening = _latest(
            rec.screening_ids.filtered(lambda s: s.verdict == "block"),
        )
        return return_Response(
            message="BLOCK sign-off rejected.",
            status=200,
            data={
                "candidate": _candidate_summary(rec),
                "screening": (
                    _screening_dict(screening, full=True) if screening else None
                ),
            },
        )

    # ------------------------------------------------------------------
    # Clarifying questions (v1.1)
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:cid>/clarifying-questions",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_clarifying_questions(self, cid, **kwargs):
        """Generate candidate-facing clarifying questions for a HOLD."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        if rec.state != "hold":
            msg = (
                "Clarifying questions are only available while the "
                "candidate is on Hold."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        clarification = rec.action_generate_clarifying_questions()
        return return_Response(
            message="Clarifying questions queued.",
            status=200,
            data={
                "clarification_id": clarification.id,
                "screening_id": clarification.screening_id.id,
                "llm_status": clarification.llm_status,
            },
        )

    @http.route(
        BASE + "/<int:cid>/decision",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_decision(self, cid, **kwargs):
        """Final decision for a scored candidate: hired or rejected."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err
        decision = (data.get("decision") or "").strip().lower()
        if decision not in FINAL_DECISIONS:
            msg = (
                f"Invalid decision '{decision}'. "
                f"Allowed: {', '.join(FINAL_DECISIONS)}."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        getattr(rec, f"action_mark_{decision}")()
        return return_Response(
            message="Decision recorded.",
            status=200,
            data={"candidate": _candidate_summary(rec)},
        )

    @http.route(
        BASE + "/<int:cid>/status",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_candidate_status(self, cid, **kwargs):
        """Polling endpoint: candidate state + per-artifact LLM statuses."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _candidate_or_404(cid)
        if err is not None:
            return err

        screening = rec.current_screening_id or _latest(rec.screening_ids)
        interview = _latest(rec.interview_ids)
        scorecard = _latest(rec.interview_ids.mapped("scorecard_ids"))
        return return_Response(
            message="OK",
            status=200,
            data={
                "id": rec.id,
                "reference": rec.reference or None,
                "state": rec.state,
                "current_verdict": rec.current_verdict or None,
                "final_recommendation": rec.final_recommendation or None,
                "hold_deadline": _iso(rec.hold_deadline),
                "block_signoff_state": (
                    screening.block_signoff_state if screening else None
                ),
                "batch": (
                    {
                        "id": rec.batch_id.id,
                        "name": rec.batch_id.name or None,
                        "state": rec.batch_id.state,
                    }
                    if rec.batch_id
                    else None
                ),
                "llm_status": {
                    "screening": screening.llm_status if screening else None,
                    "interview": interview.llm_status if interview else None,
                    "scorecard": scorecard.llm_status if scorecard else None,
                },
                "artifact_ids": {
                    "screening_id": screening.id if screening else None,
                    "interview_id": interview.id if interview else None,
                    "scorecard_id": scorecard.id if scorecard else None,
                },
            },
        )
