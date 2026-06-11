"""REST API — IRIS screening batches.

Batch CRUD + bulk candidate intake (all-or-nothing, per-index errors),
member management (draft only), screening kickoff, the consistency-review
trigger family (manual run / retry / manager re-run), status polling and
the report endpoint (404 until the batch is done). Every mutating route
calls the SAME ``action_*`` model methods as the UI buttons.
"""

import base64
import binascii
import json
import logging

from odoo import http
from odoo.http import request

from .common import (
    BATCH_STATES,
    _batch_detail,
    _batch_summary,
    _require_iris_manager,
    _require_iris_user,
    _resolve_role,
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

BASE = "/api/v1/iris/batches"

#: Per-member keys accepted in the bulk-intake ``candidates`` array.
_MEMBER_PLAIN_FIELDS = ("email", "phone", "tech_date_reference")


def _batch_or_404(bid):
    """Browse a batch by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.screening.batch"].sudo().browse(bid).exists()
    if not rec:
        return None, return_Response(
            message="Batch not found.",
            status=404,
            errors=["Batch not found."],
        )
    return rec, None


def _validate_member_entries(entries, screen_now):
    """Validate the bulk-intake ``candidates`` array UPFRONT.

    Returns ``(errors, validated)`` where ``errors`` is a list of
    per-index messages (``"candidates[2]: resume_base64 is not a PDF"``)
    and ``validated`` is a list of create-vals dicts (without role/batch).
    All-or-nothing: any error means NOTHING is created.
    """
    errors = []
    validated = []
    for index, entry in enumerate(entries):
        prefix = f"candidates[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be a JSON object.")
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            errors.append(f"{prefix}: name is required.")
            continue
        vals = {"name": name}
        for field in _MEMBER_PLAIN_FIELDS:
            if field in entry:
                vals[field] = entry[field] or False

        b64 = entry.get("resume_base64")
        filename = (entry.get("resume_filename") or "").strip()
        if b64 or filename:
            if not b64 or not filename:
                errors.append(
                    f"{prefix}: resume_base64 and resume_filename are "
                    "both required to attach a resume."
                )
                continue
            try:
                raw = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                errors.append(f"{prefix}: resume_base64 is not valid base64.")
                continue
            if not raw.startswith(b"%PDF"):
                errors.append(f"{prefix}: resume_base64 is not a PDF.")
                continue
            vals.update({"resume_file": b64, "resume_filename": filename})
        elif screen_now:
            errors.append(
                f"{prefix}: resume_base64 is required when screen_now is true."
            )
            continue
        validated.append(vals)
    return errors, validated


class IrisBatchApi(http.Controller):
    """``/api/v1/iris/batches`` endpoints."""

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_batches_list(self, **kwargs):
        """List batches with ``page``/``limit``/``state``/``role_id``/``search``."""
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        page, limit, offset = paginate(params)

        domain = []
        state = (params.get("state") or "").strip()
        if state:
            if state not in BATCH_STATES:
                msg = (
                    f"Invalid state '{state}'. "
                    f"Allowed: {', '.join(BATCH_STATES)}."
                )
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("state", "=", state))
        raw_rid = params.get("role_id")
        if raw_rid not in (None, ""):
            role_id = coerce_int(raw_rid, None)
            if role_id is None:
                msg = f"Invalid role_id '{raw_rid}'."
                return return_Response(message=msg, status=400, errors=[msg])
            domain.append(("role_id", "=", role_id))
        search = (params.get("search") or "").strip()
        if search:
            domain.append(("name", "ilike", search))

        Batch = request.env["iris.screening.batch"].sudo()
        total = Batch.search_count(domain)
        records = Batch.search(
            domain, offset=offset, limit=limit, order="id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "batches": [_batch_summary(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(BASE, type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False)
    @validate_token
    @handle_api_errors
    def iris_batches_create(self, **kwargs):
        """Create a batch with bulk candidate intake.

        Body: a role (``role_id`` / ``role_code`` / ``target_role``),
        optional ``notes`` / ``auto_run_consistency``, an optional
        ``candidates`` array (each entry: ``name`` + optional contact
        fields + optional resume pair, %PDF-validated upfront) and an
        optional ``screen_now`` flag that chains the kickoff. Member
        validation is ALL-OR-NOTHING with per-index error messages.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        data, err = read_json_body()
        if err is not None:
            return err

        role, err = _resolve_role(data, required=True)
        if err is not None:
            return err

        entries = data.get("candidates")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            msg = "candidates must be an array of candidate objects."
            return return_Response(message=msg, status=400, errors=[msg])
        screen_now = coerce_bool(data.get("screen_now"), False)

        errors, validated = _validate_member_entries(entries, screen_now)
        if errors:
            return return_Response(
                message="Batch member validation failed.",
                status=400,
                errors=errors,
            )
        if screen_now and len(validated) < 2:
            msg = "screen_now requires at least 2 candidates."
            return return_Response(message=msg, status=400, errors=[msg])

        batch_vals = {"role_id": role.id}
        if "notes" in data:
            batch_vals["notes"] = data["notes"] or False
        auto_run = coerce_bool(data.get("auto_run_consistency"), None)
        if auto_run is not None:
            batch_vals["auto_run_consistency"] = auto_run

        batch = request.env["iris.screening.batch"].sudo().create(batch_vals)
        Candidate = request.env["iris.candidate"].sudo()
        for vals in validated:
            Candidate.create({
                **vals,
                "role_id": role.id,
                "batch_id": batch.id,
            })
        if screen_now:
            batch.action_screen_batch()
        return return_Response(
            message="Batch created.",
            status=200,
            data={"batch": _batch_detail(batch)},
        )

    # ------------------------------------------------------------------
    # Item
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:bid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_batch_detail(self, bid, **kwargs):
        """Batch detail: members + per-member screening status + costs."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"batch": _batch_detail(rec)},
        )

    @http.route(
        BASE + "/<int:bid>",
        type="http", auth="none", methods=["PUT"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_update(self, bid, **kwargs):
        """Update ``name`` / ``notes`` / ``auto_run_consistency``.

        The role is changeable in Draft only (resolved like everywhere
        else: ``role_id`` / ``role_code`` / ``target_role``).
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        data, err = read_json_body()
        if err is not None:
            return err

        vals = {}
        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                msg = "name cannot be empty."
                return return_Response(message=msg, status=400, errors=[msg])
            vals["name"] = name
        if "notes" in data:
            vals["notes"] = data["notes"] or False
        auto_run = coerce_bool(data.get("auto_run_consistency"), None)
        if "auto_run_consistency" in data and auto_run is not None:
            vals["auto_run_consistency"] = auto_run

        if any(key in data for key in ("role_id", "role_code", "target_role")):
            if rec.state != "draft":
                msg = "The batch role can only be changed while in Draft."
                return return_Response(message=msg, status=400, errors=[msg])
            role, err = _resolve_role(data)
            if err is not None:
                return err
            if role is not None:
                vals["role_id"] = role.id

        if not vals:
            msg = "No updatable fields provided."
            return return_Response(message=msg, status=400, errors=[msg])
        rec.write(vals)
        return return_Response(
            message="Batch updated.",
            status=200,
            data={"batch": _batch_detail(rec)},
        )

    @http.route(
        BASE + "/<int:bid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_delete(self, bid, **kwargs):
        """Delete a batch (manager only; blocked while in flight)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Batch deleted.", status=200)

    # ------------------------------------------------------------------
    # Members (draft only)
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:bid>/candidates",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_add_members(self, bid, **kwargs):
        """Attach EXISTING draft candidates: ``{"candidate_ids": [...]}``.

        Each candidate must be in Draft, share the batch role and not
        already belong to another batch.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        if rec.state != "draft":
            msg = "Members can only be added while the batch is in Draft."
            return return_Response(message=msg, status=400, errors=[msg])
        data, err = read_json_body()
        if err is not None:
            return err

        raw_ids = data.get("candidate_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            msg = "candidate_ids must be a non-empty array of candidate ids."
            return return_Response(message=msg, status=400, errors=[msg])

        Candidate = request.env["iris.candidate"].sudo()
        errors = []
        to_attach = Candidate
        for index, raw in enumerate(raw_ids):
            prefix = f"candidate_ids[{index}]"
            cand_id = coerce_int(raw, None)
            candidate = Candidate.browse(cand_id).exists() if cand_id else None
            if not candidate:
                errors.append(f"{prefix}: candidate '{raw}' not found.")
                continue
            if candidate.batch_id and candidate.batch_id != rec:
                errors.append(
                    f"{prefix}: {candidate.name} already belongs to batch "
                    f"{candidate.batch_id.name}."
                )
                continue
            if candidate.state != "draft":
                errors.append(
                    f"{prefix}: {candidate.name} is not in Draft "
                    f"(current: {candidate.state})."
                )
                continue
            if candidate.role_id != rec.role_id:
                errors.append(
                    f"{prefix}: {candidate.name} does not target the batch "
                    f"role ({rec.role_id.name})."
                )
                continue
            to_attach |= candidate
        if errors:
            return return_Response(
                message="Member validation failed.",
                status=400,
                errors=errors,
            )
        to_attach.write({"batch_id": rec.id})
        return return_Response(
            message="Members added.",
            status=200,
            data={"batch": _batch_detail(rec)},
        )

    @http.route(
        BASE + "/<int:bid>/candidates/<int:cid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_remove_member(self, bid, cid, **kwargs):
        """Detach a member from a Draft batch (the candidate survives)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        if rec.state != "draft":
            msg = "Members can only be removed while the batch is in Draft."
            return return_Response(message=msg, status=400, errors=[msg])
        candidate = request.env["iris.candidate"].sudo().browse(cid).exists()
        if not candidate or candidate.batch_id != rec:
            msg = "Candidate is not a member of this batch."
            return return_Response(message=msg, status=404, errors=[msg])
        candidate.write({"batch_id": False})
        return return_Response(
            message="Member removed.",
            status=200,
            data={"batch": _batch_detail(rec)},
        )

    # ------------------------------------------------------------------
    # Pipeline triggers
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:bid>/screen",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_screen(self, bid, **kwargs):
        """Kick off batch screening (loops the candidate pipeline)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        rec.action_screen_batch()
        return return_Response(
            message="Batch screening started.",
            status=200,
            data={"batch": _batch_detail(rec)},
        )

    @http.route(
        BASE + "/<int:bid>/consistency",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_consistency(self, bid, **kwargs):
        """Consistency-review trigger family, dispatched on batch state.

        * ``screening`` → manual trigger (all members must have settled;
          covers manual mode / missing-key catch-up),
        * ``failed``    → retry the failed pass,
        * ``done``      → manager-only re-run after late re-screens
          (the prior report is kept as an attachment revision).
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err

        if rec.state == "screening":
            rec.action_run_consistency()
            message = "Consistency review queued."
        elif rec.state == "failed":
            rec.action_retry_llm()
            message = "Consistency review retry queued."
        elif rec.state == "done":
            guard = _require_iris_manager()
            if guard is not None:
                return guard
            rec.action_rerun_consistency()
            message = "Consistency review re-run queued."
        else:
            msg = (
                "The consistency review cannot be triggered while the "
                f"batch is in {rec.state}."
            )
            return return_Response(message=msg, status=400, errors=[msg])
        return return_Response(
            message=message,
            status=200,
            data={
                "batch_id": rec.id,
                "state": rec.state,
                "consistency_llm_status": rec.llm_status,
            },
        )

    @http.route(
        BASE + "/<int:bid>/cancel",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_batch_cancel(self, bid, **kwargs):
        """Cancel a batch (draft / screening only — model-guarded)."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        rec.action_cancel()
        return return_Response(
            message="Batch cancelled.",
            status=200,
            data={"batch": _batch_summary(rec)},
        )

    # ------------------------------------------------------------------
    # Polling + report
    # ------------------------------------------------------------------
    @http.route(
        BASE + "/<int:bid>/status",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_batch_status(self, bid, **kwargs):
        """Polling endpoint: state + member settlement + consistency status."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={
                "id": rec.id,
                "name": rec.name or None,
                "state": rec.state,
                "members": {
                    "total": rec.member_count,
                    "settled": rec.settled_count,
                    "blocking": [
                        {
                            "id": c.id,
                            "reference": c.reference or None,
                            "name": c.name or None,
                            "state": c.state,
                        }
                        for c in rec.blocking_candidate_ids
                    ],
                },
                "consistency_llm_status": rec.llm_status,
                "total_cost_usd": rec.total_cost_usd or 0.0,
            },
        )

    @http.route(
        BASE + "/<int:bid>/report",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_batch_report(self, bid, **kwargs):
        """Consistency report + parsed machine summary — 404 until done."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _batch_or_404(bid)
        if err is not None:
            return err
        if rec.state != "done" or not (rec.batch_report_markdown or "").strip():
            msg = "The consistency report is not available until the batch is done."
            return return_Response(message=msg, status=404, errors=[msg])

        machine_summary = None
        if rec.consistency_findings_json:
            try:
                machine_summary = json.loads(rec.consistency_findings_json)
            except (TypeError, ValueError):
                machine_summary = None
        return return_Response(
            message="OK",
            status=200,
            data={
                "batch_id": rec.id,
                "name": rec.name or None,
                "report_markdown": rec.batch_report_markdown,
                "machine_summary": machine_summary,
                "inconsistency_count": rec.inconsistency_count or 0,
                "revision_advisory_count": rec.revision_advisory_count or 0,
            },
        )
