import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .project_budget import REQUEST_MODEL, _request_to_dict

_logger = logging.getLogger(__name__)


class EtpBudgetRequestDetailController(http.Controller):
    """Detail view for a single batch budget request.

    Companion to /api/v1/etp_projects/budget_request/list — returns the full
    serialized record for one request, selected by ?id=<int>.
    """

    # GET — detail of one batch budget request.
    #   ?id=<int>   (the etp.batch.budget.request record id)
    @http.route(
        "/api/v1/etp_projects/budget_request/detail",
        methods=["GET"], type="http", auth="none", csrf=False, cors="*",
    )
    @validate_token
    def budget_request_detail(self, **params):
        raw_id = params.get("id")
        try:
            request_id = int(raw_id)
        except (TypeError, ValueError):
            return return_Response(
                message="'id' query parameter is required and must be an integer.",
                status=400,
            )

        req = request.env[REQUEST_MODEL].sudo().browse(request_id)
        if not req.exists():
            return return_Response(
                message="Budget request %s not found." % request_id, status=404,
            )

        return return_Response(
            message="OK", status=200,
            data={"data": _request_to_dict(req)},
        )
