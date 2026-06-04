# -*- coding: utf-8 -*-
"""Bulk-import endpoints for prompt_qc (Stage 2).

Two JSON routes back the "Bulk Import" modal:

- POST /prompt_qc/bulk/config  -> the limits the modal must respect (max rows, max file size).
- POST /prompt_qc/bulk/start   -> validate rows, create one 'queued' prompt.qc.run per valid
                                  row, and kick the background dispatcher. NO LLM call happens
                                  in this request: it only creates records and returns fast, so
                                  the web worker thread is never held by a Bedrock call.

Validation is non-raising and per-row: an invalid row never rolls back the valid ones. If ANY
row is invalid the request creates nothing and returns the per-row errors, so the user fixes
those rows (the valid ones stay populated in the modal) and re-submits — this avoids creating
duplicate runs on the retry. Each created run is then processed as an independent background
job by the worker in models/prompt_qc_run.py.
"""
import logging

from odoo import _, http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Hard cap on rows per bulk submit (mirrors MAX_ROWS in bulk_import_dialog.js). Without it a
# logged-in user could submit an arbitrarily large payload and exhaust server memory.
_MAX_BULK_ROWS = 200
# Absolute ceiling on the COMBINED base64 of one submit, independent of row count. ~256 MB of
# base64 ≈ ~192 MB of decoded files — large enough for a full 200-row batch of normal rubrics,
# small enough to stay under typical reverse-proxy body limits and to bound peak server memory.
_MAX_TOTAL_B64_CHARS = 256 * 1024 * 1024


class PromptQcBulkController(http.Controller):

    @http.route("/prompt_qc/bulk/config", type="jsonrpc", auth="user", methods=["POST"])
    def bulk_config(self, **_kw):
        Model = request.env["prompt.qc.run"]
        return {
            "max_file_size_mb": Model._get_max_file_size_mb(),
        }

    @http.route("/prompt_qc/bulk/start", type="jsonrpc", auth="user", methods=["POST"])
    def bulk_start(self, rows=None, **_kw):
        Model = request.env["prompt.qc.run"]
        rows = rows or []
        if not isinstance(rows, list):
            return {"error": _("Malformed request: 'rows' must be a list.")}

        # Row-count cap: bound the work before touching any payload.
        if len(rows) > _MAX_BULK_ROWS:
            return {"error": _(
                "You can import at most %(max)s rows at a time (you sent %(n)s). "
                "Split the import into smaller batches."
            ) % {"max": _MAX_BULK_ROWS, "n": len(rows)}}

        max_file_bytes = Model._get_max_file_size_mb() * 1024 * 1024

        # Absolute aggregate guard: reject a hostile payload by summed base64 length BEFORE any
        # per-row base64 decode. Bounded by a fixed ceiling (NOT per_file_cap * len(rows), which
        # could never trip since each row is already individually capped). Per-row validation
        # still checks every file authoritatively below.
        total_b64 = sum(
            len(r.get("rubric_b64") or "")
            for r in rows if isinstance(r, dict)
        )
        if total_b64 > _MAX_TOTAL_B64_CHARS:
            return {"error": _("Upload payload too large. Reduce the number or size of rubric files.")}

        valid_vals = []
        errors = []  # [{"row": <1-based index>, "message": "..."}]
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append({"row": idx + 1, "message": _("Malformed row.")})
                continue
            vals, err = Model._validate_bulk_row(row, max_file_bytes)
            if err:
                errors.append({"row": idx + 1, "message": err})
            elif vals:
                valid_vals.append(vals)
            # (None, None) -> empty row, silently skipped

        # Any invalid row -> create nothing; let the user fix and re-submit (no duplicates).
        if errors:
            return {"created": 0, "ids": [], "errors": errors}
        if not valid_vals:
            return {"created": 0, "ids": [],
                    "errors": [{"row": 0, "message": _("Add at least one row with a user prompt.")}]}

        # Per-user concurrency cap: refuse the WHOLE submit if it would push the user past
        # _MAX_USER_INFLIGHT queued+running runs. Single-form 'Start QC' is gated by the same
        # helper (in action_enqueue), so the two entry points share one ceiling.
        try:
            Model._check_user_inflight_cap(len(valid_vals))
        except Exception as exc:
            msg = exc.args[0] if getattr(exc, "args", None) else str(exc)
            return {"created": 0, "ids": [], "errors": [{"row": 0, "message": msg}]}

        # Create in bounded batches so a very large submit is not one giant in-memory create().
        runs = Model.create_runs_chunked(valid_vals)
        # Kick the worker AFTER this request commits, so the 'queued' rows are durably visible.
        Model._enqueue_dispatch()
        _logger.info("prompt_qc: bulk-created %s queued run(s): %s", len(runs), runs.ids)
        return {"created": len(runs), "ids": runs.ids, "errors": []}
