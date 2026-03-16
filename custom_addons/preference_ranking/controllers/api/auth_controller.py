# -*- coding: utf-8 -*-
"""
Authentication API endpoints.

POST /api/v1/auth/login     — Authenticate and receive JWT tokens
POST /api/v1/auth/logout    — (no-op for stateless JWT; included for convention)
GET  /api/v1/auth/me        — Get current user profile + role
POST /api/v1/auth/refresh   — Exchange refresh token for new access token
"""
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessDenied

from .helpers import (
    create_access_token,
    create_refresh_token,
    decode_token,
    json_error,
    json_success,
    jwt_required,
    parse_json_body,
    _resolve_role,
)

_logger = logging.getLogger(__name__)


class AuthController(http.Controller):
    """REST authentication endpoints."""

    # ------------------------------------------------------------------
    # A1: Login
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/auth/login",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def login(self, **kw):
        """Authenticate user and return JWT tokens.

        Body: {"login": "...", "password": "..."}
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        body = parse_json_body()
        login = body.get("login", "").strip()
        password = body.get("password", "")

        if not login or not password:
            return json_error("login and password are required", 400)

        try:
            env = request.env
            credential = {
                "type": "password",
                "login": login,
                "password": password,
            }
            auth_info = request.session.authenticate(env, credential)
            uid = auth_info.get("uid")

            if not uid or uid != request.session.uid:
                return json_error("Invalid credentials", 401)

        except AccessDenied:
            return json_error("Invalid credentials", 401)
        except Exception as e:
            _logger.error("Login error: %s", e)
            return json_error("Authentication failed", 500)

        user = request.env["res.users"].sudo().browse(uid)
        if not user.exists():
            return json_error("User not found", 401)

        # Generate tokens
        access_token = create_access_token(user)
        refresh_token = create_refresh_token(user)
        role = _resolve_role(user)

        employee = request.env["hr.employee"].sudo().search(
            [("user_id", "=", uid)], limit=1
        )

        return json_success(
            data={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "user": {
                    "id": user.id,
                    "name": user.name,
                    "login": user.login,
                    "role": role,
                    "employee_id": employee.id if employee else None,
                    "employee_name": employee.name if employee else user.name,
                },
            },
            message="Login successful",
        )

    # ------------------------------------------------------------------
    # A2: Logout
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/auth/logout",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def logout(self, **kw):
        """Logout (stateless JWT — client should discard the token)."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)
        return json_success(message="Logged out successfully")

    # ------------------------------------------------------------------
    # A3: Me — current user profile
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/auth/me",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    @jwt_required()
    def me(self, **kw):
        """Return current user profile and role."""
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        uid = request.jwt_uid
        env = request.jwt_env
        user = env["res.users"].browse(uid)

        employee = env["hr.employee"].sudo().search(
            [("user_id", "=", uid)], limit=1
        )

        return json_success(data={
            "id": user.id,
            "name": user.name,
            "login": user.login,
            "role": request.jwt_payload.get("role"),
            "employee_id": employee.id if employee else None,
            "employee_name": employee.name if employee else user.name,
        })

    # ------------------------------------------------------------------
    # A4: Refresh token
    # ------------------------------------------------------------------
    @http.route(
        "/api/v1/auth/refresh",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def refresh(self, **kw):
        """Exchange a refresh token for a new access token.

        Body: {"refresh_token": "..."}
        """
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        body = parse_json_body()
        refresh_token = body.get("refresh_token", "")
        if not refresh_token:
            return json_error("refresh_token is required", 400)

        payload = decode_token(refresh_token, expected_type="refresh")
        if not payload:
            return json_error("Invalid or expired refresh token", 401)

        uid = payload["sub"]
        user = request.env["res.users"].sudo().browse(uid)
        if not user.exists():
            return json_error("User not found", 401)

        new_access = create_access_token(user)
        new_refresh = create_refresh_token(user)

        return json_success(data={
            "access_token": new_access,
            "refresh_token": new_refresh,
        }, message="Token refreshed")
