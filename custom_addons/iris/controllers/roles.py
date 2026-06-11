"""REST API — IRIS role profiles (READ-ONLY).

Role creation is locked in v1.1 (Python ``create()`` guard keyed on the
``iris.enable_role_creation`` config parameter), so the API deliberately
exposes NO mutation routes — only the list (with the ``creation_locked``
envelope flag) and the detail. Per-role prompt overrides are never exposed.
"""

import logging

from odoo import http
from odoo.http import request

from .common import (
    _require_iris_user,
    _role_dict,
    coerce_bool,
    handle_api_errors,
)
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

BASE = "/api/v1/iris/roles"


def _role_or_404(rid):
    """Browse a role profile by id → ``(record, None)`` or ``(None, 404)``."""
    rec = (
        request.env["iris.role.profile"]
        .sudo()
        .with_context(active_test=False)
        .browse(rid)
        .exists()
    )
    if not rec:
        return None, return_Response(
            message="Role profile not found.",
            status=404,
            errors=["Role profile not found."],
        )
    return rec, None


class IrisRoleApi(http.Controller):
    """``/api/v1/iris/roles`` endpoints (read-only)."""

    @http.route(BASE, type="http", auth="none", methods=["GET"], csrf=False, cors="*")
    @validate_token
    @handle_api_errors
    def iris_roles_list(self, **kwargs):
        """List the selectable roles (active, non-legacy).

        ``include_inactive=1`` adds the rest (archived + legacy roles).
        The envelope carries ``creation_locked`` — True while the
        ``iris.enable_role_creation`` system parameter is unset.
        """
        guard = _require_iris_user()
        if guard is not None:
            return guard

        params = request.params or {}
        include_inactive = coerce_bool(params.get("include_inactive"), False)

        Role = (
            request.env["iris.role.profile"].sudo().with_context(active_test=False)
        )
        domain = []
        if not include_inactive:
            domain = [("active", "=", True), ("is_legacy", "=", False)]
        records = Role.search(domain, order="sequence, id")
        return return_Response(
            message="OK",
            status=200,
            data={
                "roles": [_role_dict(rec) for rec in records],
                "creation_locked": not Role._role_creation_allowed(),
            },
        )

    @http.route(
        BASE + "/<int:rid>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    @handle_api_errors
    def iris_role_detail(self, rid, **kwargs):
        """Role detail incl. competence guidance + default tech-date table."""
        guard = _require_iris_user()
        if guard is not None:
            return guard
        rec, err = _role_or_404(rid)
        if err is not None:
            return err
        return return_Response(
            message="OK",
            status=200,
            data={"role": _role_dict(rec, full=True)},
        )
