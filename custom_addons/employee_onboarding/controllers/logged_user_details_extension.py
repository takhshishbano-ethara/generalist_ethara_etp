"""Override the api_auth_gateway /api/v1/get_logged_user_details endpoint
so the response carries an `onboarding_completed` flag.

Mirrors the injection done for /api/v1/auth_token so the frontend can read
the same flag from either endpoint and decide whether to show the
onboarding popup.
"""

import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.main import ApiAuthController

_logger = logging.getLogger(__name__)


class LoggedUserDetailsOnboardingExtension(ApiAuthController):

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

            # Default True - users without an employee record (eg system admins)
            # shouldn't be nagged with the onboarding popup.
            completed = True
            user = request.env.user
            if user and user.exists() and user.employee_id:
                completed = bool(user.employee_id.sudo().onboarding_completed)

            record["onboarding_completed"] = completed
            response.set_data(json.dumps(body))
        except Exception:
            _logger.exception(
                "Failed to inject onboarding_completed into /api/v1/get_logged_user_details response"
            )
        return response