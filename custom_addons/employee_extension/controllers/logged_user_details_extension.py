import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.main import ApiAuthController

_logger = logging.getLogger(__name__)


class LoggedUserDetailsCandidateExtension(ApiAuthController):

    @http.route(
        "/api/v1/get_logged_user_details",
        methods=["GET"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    def get_logged_user_details(self, **kwargs):
        response = super().get_logged_user_details(**kwargs)
        try:
            body_text = response.get_data(as_text=True)
            if not body_text:
                return response

            body = json.loads(body_text)
            if body.get("status_code") != 200:
                return response

            record = body.get("record")
            if not isinstance(record, dict):
                return response

            user = request.env.user
            if not (user and user.exists()):
                return response

            candidate_role = request.env.ref(
                "api_auth_gateway.role_candidate_technical",
                raise_if_not_found=False,
            )
            if not candidate_role or not user.user_role or user.user_role.id != candidate_role.id:
                return response

            applicant = request.env["hr.applicant"].sudo().with_context(
                active_test=False,
            ).search(
                [("candidate_user_id", "=", user.id)], limit=1,
            )
            if not applicant:
                return response

            record["candidate_id"] = applicant.id
            candidate_name = applicant.partner_name or applicant.name or ""
            if candidate_name:
                record["name"] = candidate_name

            response.set_data(json.dumps(body))
        except Exception:
            _logger.exception(
                "Failed to inject candidate info into /api/v1/get_logged_user_details response"
            )
        return response
