# -*- coding: utf-8 -*-
"""
API helpers: JWT token management, auth decorators, JSON response utilities.

All REST API endpoints share the same authentication, error handling, and
response formatting patterns defined here.
"""
import functools
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import jwt
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT Configuration
# ---------------------------------------------------------------------------
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_ACCESS_TTL = int(os.environ.get("JWT_ACCESS_TTL", 86400))       # 24h
JWT_REFRESH_TTL = int(os.environ.get("JWT_REFRESH_TTL", 604800))    # 7d


def _get_jwt_secret():
    """Return the signing key, falling back to a hash of the Odoo master pwd."""
    if JWT_SECRET:
        return JWT_SECRET
    # Deterministic fallback so all workers share the same key
    from odoo.tools import config
    master = config.get("admin_passwd", "admin") or "admin"
    return hashlib.sha256(f"preference-ranking-jwt-{master}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(user):
    """Create an access JWT for the given ``res.users`` record."""
    now = int(time.time())
    role = _resolve_role(user)
    payload = {
        "sub": str(user.id),
        "login": user.login,
        "name": user.name,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + JWT_ACCESS_TTL,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user):
    """Create a refresh JWT (long-lived, used only to get a new access token)."""
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "type": "refresh",
        "iat": now,
        "exp": now + JWT_REFRESH_TTL,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token, expected_type="access"):
    """Decode and verify a JWT. Returns the payload dict or None."""
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != expected_type:
            return None
        # Convert sub back to int for Odoo uid usage
        payload["sub"] = int(payload["sub"])
        return payload
    except jwt.ExpiredSignatureError:
        _logger.warning("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        _logger.warning("JWT invalid: %s", e)
        return None


# ---------------------------------------------------------------------------
# Role resolver
# ---------------------------------------------------------------------------

def _resolve_role(user):
    """Return the highest Vindex role string for the user."""
    if user.has_group("preference_ranking.group_vindex_admin"):
        return "admin"
    if user.has_group("preference_ranking.group_vindex_ql"):
        return "quality_lead"
    if user.has_group("preference_ranking.group_vindex_tasker"):
        return "tasker"
    return "user"


# ---------------------------------------------------------------------------
# JSON response builders
# ---------------------------------------------------------------------------

def json_response(data, status=200):
    """Return a well-formed JSON HTTP response."""
    body = json.dumps(data, default=str)
    return http.Response(
        body,
        content_type="application/json",
        status=status,
    )


def json_success(data=None, message="Success", status=200, **extra):
    """Standard success envelope."""
    payload = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return json_response(payload, status=status)


def json_error(message="Error", status=400, errors=None):
    """Standard error envelope."""
    payload = {"success": False, "message": message}
    if errors:
        payload["errors"] = errors
    return json_response(payload, status=status)


# ---------------------------------------------------------------------------
# Request body parser
# ---------------------------------------------------------------------------

def parse_json_body():
    """Parse JSON from the request body. Returns dict or empty dict."""
    try:
        raw = request.httprequest.get_data(as_text=True)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def jwt_required(roles=None):
    """
    Decorator for Odoo HTTP controllers that enforces JWT auth.

    Usage::

        @http.route('/api/v1/tasks', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
        @jwt_required(roles=['tasker', 'quality_lead', 'admin'])
        def list_tasks(self, **kw):
            user_payload = request.jwt_payload  # injected by decorator
            ...

    Args:
        roles: list of allowed role strings. None = any authenticated user.
    """
    if roles and isinstance(roles, str):
        roles = [roles]

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            auth_header = request.httprequest.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return json_error("Missing or malformed Authorization header", 401)

            token = auth_header[7:]
            payload = decode_token(token, expected_type="access")
            if not payload:
                return json_error("Invalid or expired token", 401)

            if roles and payload.get("role") not in roles:
                return json_error("Insufficient permissions", 403)

            # Hydrate the Odoo environment for this user
            uid = payload["sub"]
            try:
                # With auth='none', request.env may not have a db cursor.
                # We need to ensure we have a proper environment.
                import odoo
                db = request.db
                if not db:
                    from odoo.tools import config
                    db = config.get("db_name")
                if not db:
                    return json_error("Database not configured", 500)

                registry = odoo.modules.registry.Registry(db)
                cr = registry.cursor()
                env = odoo.api.Environment(cr, uid, {})
                user = env["res.users"].browse(uid)
                if not user.exists():
                    cr.close()
                    return json_error("User not found", 401)
            except Exception as e:
                _logger.error("JWT env hydration error: %s", e)
                return json_error("Authentication error", 401)

            # Inject into request for downstream use
            request.jwt_payload = payload
            request.jwt_uid = uid
            request.jwt_env = env
            request._jwt_cr = cr  # store cursor for cleanup

            try:
                result = func(self, *args, **kwargs)
                cr.close()
                return result
            except Exception as e:
                cr.close()
                raise
        return wrapper
    return decorator


def admin_only(func):
    """Shorthand decorator: JWT required + admin role only."""
    return jwt_required(roles=["admin"])(func)


def ql_or_admin(func):
    """Shorthand decorator: JWT required + quality_lead or admin."""
    return jwt_required(roles=["quality_lead", "admin"])(func)


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

def paginate_params(params):
    """Extract and validate pagination params from query string."""
    try:
        page = max(1, int(params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        limit = min(100, max(1, int(params.get("limit", 20))))
    except (ValueError, TypeError):
        limit = 20
    offset = (page - 1) * limit
    return page, limit, offset
