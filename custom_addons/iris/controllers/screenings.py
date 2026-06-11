"""REST API — IRIS screenings.

Read-only access to screening records (list + full markdown record with
verdict, evidence and LLM metadata) plus manager-only delete.
"""

import logging

from odoo import http
from odoo.http import request

from .common import (
    _require_iris_manager,
    _require_iris_user,
    _screening_dict,
    coerce_int,
    handle_api_errors,
    paginate,
    pagination_block,
)
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

BASE = "/api/v1/iris/screenings"


def _screening_or_404(sid):
    """Browse a screening by id → ``(record, None)`` or ``(None, 404)``."""
    rec = request.env["iris.screening"].sudo().browse(sid).exists()
    if not rec:
        return None, return_Response(
            message="Screening not found.",
            status=404,
            errors=["Screening not found."],
        )
    return rec, None


class IrisScreeningApi(http.Controller):
    """``/api/v1/iris/screenings`` endpoints."""

    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_screenings_list(self, **kwargs):
        """List screenings; optional ``candidate_id`` filter + pagination."""
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

        Screening = request.env["iris.screening"].sudo()
        total = Screening.search_count(domain)
        records = Screening.search(
            domain, offset=offset, limit=limit, order="id desc",
        )
        return return_Response(
            message="OK",
            status=200,
            data={
                "screenings": [_screening_dict(rec) for rec in records],
                "pagination": pagination_block(total, page, limit),
            },
        )

    @http.route(
        BASE + "/<int:sid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_screening_detail(self, sid, **kwargs):
        """Full screening record: markdown, verdict, evidence, LLM metadata."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _screening_or_404(sid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"screening": _screening_dict(rec, full=True)},
        )

    @http.route(
        BASE + "/<int:sid>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*", readonly=False,
    )
    @validate_token
    @handle_api_errors
    def iris_screening_delete(self, sid, **kwargs):
        """Delete a screening (manager only)."""
        guard = _require_iris_manager()
        if guard is not None:
            return guard
        rec, err = _screening_or_404(sid)
        if err is not None:
            return err
        rec.unlink()
        return return_Response(message="Screening deleted.", status=200)
