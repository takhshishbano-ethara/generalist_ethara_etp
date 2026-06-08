"""Shared helpers for the Aurora Extension controllers."""

from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response

# Fixed PR-range buckets used as filter options on the showcase dashboard.
PR_RANGES = ["2-5", "6-10", "11-20", "21-40", "41-100"]

# Instance lifecycle states grouped for KPI / quality-tier reporting.
IN_PROGRESS_STATES = ("pending", "building", "built", "running")


def require_aurora_user():
    """Return a 403 Response if the caller is not an Aurora user, else None."""
    if not request.env.user.has_group("aurora.group_aurora_user"):
        return return_Response(
            message="You are not allowed to access Aurora data.",
            status=403,
        )
    return None


def user_role_tag(env):
    """Coarse role tag for the caller, mirroring talos_extension's `role`.

    Returns "admin" / "user", or None if the caller has no Aurora access.
    """
    user = env.user
    if user.has_group("aurora.group_aurora_admin"):
        return "admin"
    if user.has_group("aurora.group_aurora_user"):
        return "user"
    return None


def coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pct(part, whole):
    if whole == 0:
        return 0.0
    return round((part / whole) * 100.0, 2)
