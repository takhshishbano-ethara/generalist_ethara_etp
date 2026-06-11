"""REST API — IRIS job descriptions (JD critique + rewrite pre-stage).

JD CRUD (raw text or PDF source), the two LLM triggers (critique, rewrite),
manager approval (hard-blocked while ``[FILL-IN`` placeholders remain — the
model enforces it) and status polling. Every trigger calls the SAME
``action_*`` model methods as the UI buttons.
"""

import base64
import binascii
import logging

from odoo import http
from odoo.http import request

from .common import (
    JD_STATES,
    _jd_artifact_dict,
    _jd_detail,
    _jd_summary,
    _require_iris_manager,
    _require_iris_user,
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

BASE = "/api/v1/iris/jds"

#: Fields editable only while the JD is in Draft.
_DRAFT_ONLY_FIELDS = ("name", "company_name", "raw_jd")


def _jd_or_404(jid):
    """Browse a job description by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.job.description"].sudo().browse(jid).exists()
    if not rec:
        return None, return_Response(
            message="Job description not found.",
            status=404,
            errors=["Job description not found."],
        )
    return rec, None


def _source_vals(data):
    """Validate the optional source-file pair → ``(vals, None)`` / ``(None, 400)``."""
    b64 = data.get("source_base64")
    filename = (data.get("source_filename") or "").strip()
    if not b64 and not filename:
        return {}, None
    if not b64 or not filename:
        msg = "source_base64 and source_filename are both required."
        return None, return_Response(message=msg, status=400, errors=[msg])
    try:
        base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        msg = "source_base64 is not valid base64."
        return None, return_Response(message=msg, status=400, errors=[msg])
    return {"source_file": b64, "source_filename": filename}, None


def _latest_artifact(rec, operation):
    """Latest ``iris.jd.artifact`` of ``operation`` regardless of status."""
    return rec.artifact_ids.filtered(
        lambda a: a.operation == operation
    ).sorted("id", reverse=True)[:1]


class IrisJdApi(http.Controller):
    """``/api/v1/iris/jds`` endpoints."""

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_jds_list(self, **kwargs):
        """List JDs with ``page``/``limit``/``state``/``search``."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        state = (params.get("state") or "").strip()
        if state:
            if state not in JD_STATES:
                msg = (
                    f"Invalid state '{state}'. Allowed: {', '.join(JD_STATES)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("state", "=", state))
        search = (params.get("search") or "").strip()
        if search:
            domain += [
                "|",
                ("name", "ilike", search),
                ("company_name", "ilike", search),
            ]

        Jd = request.env["iris.job.description"].sudo()
        total = Jd.search_count(domain)
        records = Jd.search(domain, offset=offset, limit=limit, order="id desc")
        return return_Response(
            message="OK",
            status=200,
            data={
                "jds": [_jd_summary(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(BASE, type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False)
    @validate_token
    @handle_api_errors
    def iris_jds_create(self, **kwargs):
        """Create a JD; ``name`` required, ``raw_jd`` or a PDF source.

        When ``raw_jd`` is empty and a source PDF is supplied, the model
        extracts the text automatically.
        """
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

        vals = {"name": name}
        if "company_name" in data:
            vals["company_name"] = data["company_name"] or False
        if "raw_jd" in data:
            vals["raw_jd"] = data["raw_jd"] or False

        source_vals, err = _source_vals(data)
        if err is not None:
            return err
        vals.update(source_vals)

        rec = request.env["iris.job.description"].sudo().create(vals)
        return return_Response(
            message="Job description created.",
            status=200,
            data={"jd": _jd_detail(rec)},
        )

    # ------------------------------------------------------------------
    # Item
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:jid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_jd_detail(self, jid, **kwargs):
        """Full JD detail incl. artifact summaries."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"jd": _jd_detail(rec)},
        )

    @http.route(
        BASE + "/<int:jid>",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_jd_update(self, jid, **kwargs):
        """Update a JD: ``name``/``company_name``/``raw_jd`` draft-only;
        ``final_jd`` editable until approved."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        if rec.state != "draft" and any(
            field in data for field in _DRAFT_ONLY_FIELDS
        ):
            msg = (
                "name, company_name and raw_jd can only be edited while "
                "the job description is in Draft."
            )
            return return_Response(message=msg, status=400, errors=[msg])

        vals = {}
        for field in _DRAFT_ONLY_FIELDS:
            if field in data:
                vals[field] = data[field] or False
        if "name" in vals and not vals["name"]:
            msg = "name cannot be empty."
            return return_Response(message=msg, status=400, errors=[msg])

        if "final_jd" in data:
            if rec.state == "approved":
                msg = (
                    "The Final JD is locked after approval — reopen the "
                    "job description first."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            vals["final_jd"] = data["final_jd"] or False

        if not vals:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])
        rec.write(vals)
        return return_Response(
            message="Job description updated.",
            status=200,
            data={"jd": _jd_detail(rec)},
        )

    @http.route(
        BASE + "/<int:jid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_jd_delete(self, jid, **kwargs):
        """Delete a JD (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Job description deleted.", status=200)

    # ------------------------------------------------------------------
    # LLM triggers + approval
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:jid>/critique",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_jd_critique(self, jid, **kwargs):
        """Run (or re-run) the LLM critique (async — poll /status)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        artifact = rec.action_critique()
        return return_Response(
            message="Critique queued.",
            status=200,
            data={
                "artifact_id": artifact.id,
                "operation": artifact.operation,
                "llm_status": artifact.llm_status,
                "state": rec.state,
            },
        )

    @http.route(
        BASE + "/<int:jid>/rewrite",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_jd_rewrite(self, jid, **kwargs):
        """Run (or re-run) the LLM rewrite — requires a completed critique."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        artifact = rec.action_rewrite()
        return return_Response(
            message="Rewrite queued.",
            status=200,
            data={
                "artifact_id": artifact.id,
                "operation": artifact.operation,
                "llm_status": artifact.llm_status,
                "state": rec.state,
            },
        )

    @http.route(
        BASE + "/<int:jid>/approve",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_jd_approve(self, jid, **kwargs):
        """Approve a rewritten JD (manager; 400 while [FILL-IN remains)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err
        rec.action_approve()
        return return_Response(
            message="Job description approved.",
            status=200,
            data={"jd": _jd_detail(rec)},
        )

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:jid>/status",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_jd_status(self, jid, **kwargs):
        """Polling endpoint: JD state + per-operation artifact statuses."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _jd_or_404(jid)
        if err is not None:
            return err

        critique = _latest_artifact(rec, "critique")
        rewrite = _latest_artifact(rec, "rewrite")
        return return_Response(
            message="OK",
            status=200,
            data={
                "id": rec.id,
                "state": rec.state,
                "has_fillins": bool(rec.has_fillins),
                "llm_status": {
                    "critique": critique.llm_status if critique else None,
                    "rewrite": rewrite.llm_status if rewrite else None,
                },
                "artifact_ids": {
                    "critique_id": critique.id if critique else None,
                    "rewrite_id": rewrite.id if rewrite else None,
                },
                "artifacts": [
                    _jd_artifact_dict(a) for a in rec.artifact_ids
                ],
            },
        )
