"""REST controller for the Employee Role Import module.

Route prefix
------------
``/api/v2/employee-role-import/...``

Authentication
--------------
All routes use ``@validate_token`` from ``api_auth_gateway``.  Callers
send an ``access-token`` HTTP header issued by that module.  The
controller never reads the session directly - everything happens
through the resolved ``request.env.user``.

Response envelope
-----------------
The shared ``return_Response`` helper guarantees::

    {
        "message": "...",
        "errors": [],
        "status_code": 200,
        ...payload
    }

We extend it by always shipping a ``data`` dict so the client sees a
predictable shape regardless of the endpoint.
"""

import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

ROUTE_PREFIX = "/api/v2/employee-role-import"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_INTERNAL_ERROR = 500

ERR_VALIDATION = "validation_error"


def _err(message, status=HTTP_BAD_REQUEST, code=ERR_VALIDATION, details=None):
    payload = {
        "data": {
            "error_code": code,
            "details": details or {},
        },
    }
    return return_Response(message=message, status=status, data=payload)


def _ok(message, data=None, status=HTTP_OK):
    return return_Response(
        message=message,
        status=status,
        data={"data": data or {}},
    )


def _bool_param(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _int_param(value, default=None):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EmployeeRoleImportController(http.Controller):
    """REST controller covering employee list."""

    @http.route(
        f"{ROUTE_PREFIX}/employees",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_employees(self, **kwargs):
        try:
            page = _int_param(kwargs.get("page"), 1) or 1
            limit = _int_param(kwargs.get("limit"), DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE
            limit = max(1, min(limit, MAX_PAGE_SIZE))
            search = kwargs.get("search") or None
            role = (kwargs.get("role") or "").strip().lower() or None
            parent_id = _int_param(kwargs.get("parent_id"))
            active = _bool_param(kwargs.get("active"), default=True)
            include_archived = _bool_param(
                kwargs.get("include_archived"), default=False
            )
            has_user = _bool_param(kwargs.get("has_user"))
            order = kwargs.get("order") or "name asc, id asc"

            Employee = request.env["hr.employee"].sudo()
            if include_archived:
                Employee = Employee.with_context(active_test=False)

            domain = self._build_domain(
                search=search, role=role, parent_id=parent_id,
                active=active, include_archived=include_archived,
                has_user=has_user,
            )

            total = Employee.search_count(domain)
            offset = max(page - 1, 0) * limit
            employees = Employee.search(
                domain, limit=limit, offset=offset, order=order,
            )

            payload = {
                "data": [self.serialize(e) for e in employees],
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total_records": total,
                    "total_pages": (total + limit - 1) // limit if limit else 0,
                    "count_in_page": len(employees),
                },
                "filters_applied": {
                    "search": search or None,
                    "role": role or None,
                    "parent_id": parent_id,
                    "active": active,
                    "has_user": has_user,
                    "include_archived": include_archived,
                },
            }
            return _ok(_("Employees list"), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_employees")

    @staticmethod
    def _build_domain(search, role, parent_id, active,
                      include_archived, has_user):
        domain = []
        if not include_archived and active is not None:
            domain.append(("active", "=", bool(active)))
        if search:
            domain += [
                "|", "|",
                ("name", "ilike", search),
                ("work_email", "ilike", search),
                ("employee_code", "ilike", search),
            ]
        if role:
            domain.append(("user_id.user_role.name", "ilike", role))
        if parent_id:
            domain.append(("parent_id", "=", parent_id))
        if has_user is True:
            domain.append(("user_id", "!=", False))
        elif has_user is False:
            domain.append(("user_id", "=", False))
        return domain

    @staticmethod
    def serialize(emp):
        user = emp.user_id
        f = emp._fields

        def _m2o(field_name):
            if field_name not in f:
                return None, None
            value = emp[field_name]
            return (value.id, value.name or "") if value else (None, None)

        pl_id, pl_name = _m2o("task_forge_pl_id")
        qr_id, qr_name = _m2o("task_forge_qr_id")
        ql_id, ql_name = _m2o("task_forge_ql_id")
        tpm_id, tpm_name = _m2o("task_forge_tpm_id")

        return {
            "id": emp.id,
            "employee_id": emp.employee_code or "",
            "name": emp.name or "",
            "email": emp.work_email or "",
            "job_title": emp.job_title or "",
            "role_id": user.user_role.id if user and user.user_role else None,
            "role": user.user_role.name if user and user.user_role else "",
            "reports_to": {
                "id": emp.parent_id.id,
                "name": emp.parent_id.name or "",
            } if emp.parent_id else None,
            "assigned_pl_id": pl_id,
            "assigned_pl_name": pl_name,
            "assigned_qr_id": qr_id,
            "assigned_qr_name": qr_name,
            "assigned_ql_id": ql_id,
            "assigned_ql_name": ql_name,
            "assigned_tpm_id": tpm_id,
            "assigned_tpm_name": tpm_name,
            "user_id": user.id if user else None,
            "user_login": user.login if user else None,
            "active": bool(emp.active),
            "status": "active" if emp.active else "archived",
            "offboarding_state": emp.offboarding_state or "",
            "offboard_date": emp.offboard_date.isoformat() if emp.offboard_date else None,
            "created_at": emp.create_date.isoformat() if emp.create_date else None,
            "updated_at": emp.write_date.isoformat() if emp.write_date else None,
        }

    @staticmethod
    def _unexpected(exc, op):
        if isinstance(exc, AccessError):
            _logger.info("employee_role_import_api.%s access denied: %s", op, exc)
            return _err(
                str(exc.args[0] if exc.args else exc),
                status=HTTP_FORBIDDEN,
                code="forbidden",
            )
        if isinstance(exc, (UserError, ValidationError)):
            _logger.info("employee_role_import_api.%s user error: %s", op, exc)
            return _err(
                str(exc.args[0] if exc.args else exc),
                status=HTTP_BAD_REQUEST,
                code="validation_error",
            )
        _logger.exception("employee_role_import_api.%s unexpected error", op)
        return _err(
            _("Unexpected server error in %s.") % op,
            status=HTTP_INTERNAL_ERROR,
            code="internal_error",
            details={"exception": type(exc).__name__},
        )
