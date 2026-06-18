"""Override the api_auth_gateway /api/v1/auth_token endpoint so the login
response carries an `onboarding_completed` flag.

The frontend reads this flag and decides whether to show the onboarding
popup on every login.
"""

import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.main import ApiAuthController

_logger = logging.getLogger(__name__)


class AuthTokenOnboardingExtension(ApiAuthController):

    @http.route(
        "/api/v1/auth_token",
        methods=["POST"],
        type="http",
        auth="none",
        csrf=False,
        cors="*",
    )
    def auth_token(self, **kwargs):
        response = super().auth_token(**kwargs)
        try:
            body_text = response.get_data(as_text=True)
            if not body_text:
                return response

            body = json.loads(body_text)
            if body.get("status_code") != 200 or not isinstance(body.get("data"), dict):
                return response

            uid = body["data"].get("uid")
            # Default True - users without an employee record (eg system admins)
            # shouldn't be nagged with the onboarding popup.
            completed = True
            if uid:
                user = request.env["res.users"].sudo().browse(int(uid))
                if user.exists() and user.employee_id:
                    completed = bool(user.employee_id.sudo().onboarding_completed)

            body["data"]["onboarding_completed"] = completed
            response.set_data(json.dumps(body))
        except Exception:
            _logger.exception(
                "Failed to inject onboarding_completed into /api/v1/auth_token response"
            )
        return response
