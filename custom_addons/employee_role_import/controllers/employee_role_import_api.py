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

import base64
import json
import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_request,
    validate_token,
)

from ..models.hr_employee import ROLE_GROUP_MAP, ROLE_SELECTION
from ..services.csv_import_service import CsvImportService, CsvParseError
from ..services.employee_service import EmployeeService, EmployeeServiceError
from ..services.export_service import ExportService
from ..services.response_codes import (
    DEFAULT_PAGE_SIZE,
    ERR_DEPENDENCY_MISSING,
    ERR_EXPORT_FAILED,
    ERR_NOT_FOUND,
    ERR_SESSION_EXPIRED,
    ERR_VALIDATION,
    HTTP_BAD_REQUEST,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    MAX_CSV_BYTES,
)

_logger = logging.getLogger(__name__)

ROUTE_PREFIX = "/api/v2/employee-role-import"
ROLE_LABELS = dict(ROLE_SELECTION)


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


def _service_error_response(exc: EmployeeServiceError):
    return _err(
        str(exc.args[0] if exc.args else exc),
        status=exc.http_status,
        code=exc.code,
        details=exc.details,
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
    """REST controller covering CSV import + employee CRUD + export."""

    # ── role hierarchy ───────────────────────────────────────────────────────
    @http.route(
        f"{ROUTE_PREFIX}/roles",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_roles(self, **kwargs):
        """Return the roles the authenticated user is allowed to assign."""
        try:
            allowed = (
                request.env["hr.employee"]._current_user_assignable_roles()
            )
            roles = [
                {
                    "key": k,
                    "label": ROLE_LABELS.get(k, k),
                }
                for k in allowed
            ]
            return _ok("Roles list", data={"roles": roles, "count": len(roles)})
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_roles")

    @http.route(
        f"{ROUTE_PREFIX}/roles/hierarchy",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def role_hierarchy(self, **kwargs):
        try:
            service = EmployeeService(request.env)
            return _ok("Role hierarchy", data=service.role_hierarchy())
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "role_hierarchy")

    # ── CSV upload / preview / commit ────────────────────────────────────────
    @http.route(
        f"{ROUTE_PREFIX}/upload",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def upload_csv(self, **kwargs):
        """Upload a CSV - returns a session_token + preview payload.

        Accepts two transports:

        * ``multipart/form-data`` with a ``csv_file`` file part.
        * ``application/json`` with ``{"csv_b64": "...", "filename": "..."}``.
        """
        try:
            raw_bytes, filename = self._extract_upload()
            if not raw_bytes:
                return _err(
                    _("No CSV file provided. Send 'csv_file' (multipart) "
                      "or 'csv_b64' (JSON)."),
                    status=HTTP_BAD_REQUEST,
                )
            if len(raw_bytes) > MAX_CSV_BYTES:
                return _err(
                    _("Uploaded file exceeds the %d KB limit.")
                    % (MAX_CSV_BYTES // 1024),
                    status=HTTP_BAD_REQUEST,
                )

            default_role = (request.params.get("default_role") or "").strip().lower()
            create_user = _bool_param(request.params.get("create_user"), default=True)
            default_password = (
                request.params.get("default_password") or "Ethara@123"
            )

            service = CsvImportService(request.env)
            session = service.create_session(
                raw_bytes=raw_bytes,
                filename=filename,
                default_role=default_role,
                create_user=create_user,
                default_password=default_password,
            )
            preview = service.build_preview(session)
            return _ok(
                _("CSV uploaded and previewed"),
                data={
                    "session_token": session.session_token,
                    "preview": preview,
                },
            )
        except CsvParseError as exc:
            return _err(
                str(exc.args[0] if exc.args else exc),
                status=HTTP_BAD_REQUEST,
                code="csv_parse_error",
            )
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "upload_csv")

    @http.route(
        f"{ROUTE_PREFIX}/preview/<string:session_token>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def get_preview(self, session_token, **kwargs):
        """Return a previously-uploaded session's preview."""
        try:
            session = self._fetch_session(session_token)
            if not session:
                return _err(
                    _("Session not found or expired."),
                    status=HTTP_NOT_FOUND, code=ERR_SESSION_EXPIRED,
                )
            service = CsvImportService(request.env)
            if (
                session.state in ("previewed", "imported", "failed", "discarded")
                and session.preview_payload
            ):
                payload = json.loads(session.preview_payload)
            else:
                payload = service.build_preview(session)
            return _ok(
                _("Preview"),
                data={"preview": payload, "session_state": session.state},
            )
        except CsvParseError as exc:
            return _err(str(exc), status=HTTP_BAD_REQUEST, code="csv_parse_error")
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "get_preview")

    @http.route(
        f"{ROUTE_PREFIX}/commit",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    @validate_request({
        "session_token": {"type": "string", "required": True},
    })
    def commit_import(self, **kwargs):
        """Persist the previewed rows.

        Optional ``rows`` (alias: ``row_overrides``) — list of per-row
        edits the user made in the preview UI before clicking Save.
        These are merged into the rebuilt preview so inline edits
        (name / employee_id / email / role / job_title / assigned_*)
        actually reach the DB. Without this, the commit re-reads the
        original CSV and silently drops the user's changes.
        """
        try:
            jdata = kwargs.get("jdata") or {}
            session = self._fetch_session(jdata["session_token"])
            if not session:
                return _err(
                    _("Session not found or expired."),
                    status=HTTP_NOT_FOUND, code=ERR_SESSION_EXPIRED,
                )
            row_overrides = jdata.get("rows") or jdata.get("row_overrides")
            service = CsvImportService(request.env)
            summary = service.commit(session, row_overrides=row_overrides)
            return _ok(
                _("Import complete"),
                data={"summary": summary, "session_token": session.session_token},
            )
        except CsvParseError as exc:
            return _err(str(exc), status=HTTP_BAD_REQUEST, code="csv_parse_error")
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "commit_import")

    # NOTE: this route deliberately sits at `/validate-row` instead of
    # `/preview/validate-row`. The dynamic route `/preview/<session_token>`
    # above shadows any static sibling under `/preview/...`, so werkzeug
    # matched `validate-row` as a session_token and returned 405 for POST
    # (Allow: GET, HEAD, OPTIONS). Keeping it at the top level avoids the
    # collision.
    @http.route(
        f"{ROUTE_PREFIX}/validate-row",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def validate_preview_row(self, **kwargs):
        """Re-run validation for a single edited preview row.

        Used by the Flutter import preview after the user edits
        Employee ID / Email / Role / hierarchy fields, so the row's
        status updates without re-uploading the CSV.
        """
        try:
            payload = self._read_json_body()
            service = CsvImportService(request.env)
            row = service.validate_row(payload)
            return _ok(_("Row validated"), data={"row": row})
        except CsvParseError as exc:
            return _err(str(exc), status=HTTP_BAD_REQUEST, code="csv_parse_error")
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "validate_preview_row")

    @http.route(
        f"{ROUTE_PREFIX}/sessions/<string:session_token>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*",
    )
    @validate_token
    def discard_session(self, session_token, **kwargs):
        try:
            session = self._fetch_session(session_token)
            if not session:
                return _err(
                    _("Session not found or expired."),
                    status=HTTP_NOT_FOUND, code=ERR_SESSION_EXPIRED,
                )
            service = CsvImportService(request.env)
            service.discard(session)
            return _ok(_("Session discarded"), data={"session_token": session_token})
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "discard_session")

    # ── employee CRUD ────────────────────────────────────────────────────────
    @http.route(
        f"{ROUTE_PREFIX}/employees",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_employees(self, **kwargs):
        try:
            page = _int_param(kwargs.get("page"), 1)
            limit = _int_param(kwargs.get("limit"), DEFAULT_PAGE_SIZE)
            search = kwargs.get("search") or None
            role = (kwargs.get("role") or "").strip().lower() or None
            parent_id = _int_param(kwargs.get("parent_id"))
            active = _bool_param(kwargs.get("active"), default=True)
            include_archived = _bool_param(
                kwargs.get("include_archived"), default=False
            )
            has_user = _bool_param(kwargs.get("has_user"))
            order = kwargs.get("order") or "name asc, id asc"

            service = EmployeeService(request.env)
            payload = service.list(
                page=page, limit=limit, search=search, role=role,
                parent_id=parent_id, active=active, has_user=has_user,
                order=order, include_archived=include_archived,
            )
            return _ok(_("Employees list"), data=payload)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_employees")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def get_employee(self, employee_id, **kwargs):
        try:
            service = EmployeeService(request.env)
            include_archived = _bool_param(
                kwargs.get("include_archived"), default=True
            )
            payload = service.get(employee_id, include_archived=include_archived)
            return _ok(_("Employee detail"), data={"employee": payload})
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "get_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    @validate_request({
        "name": {"type": "string", "required": True},
        "employee_id": {"type": "string", "required": True},
        "email": {"type": "email", "required": True},
    })
    def create_employee(self, **kwargs):
        try:
            payload = kwargs.get("jdata") or {}
            service = EmployeeService(request.env)
            result = service.create(payload)
            return _ok(
                _("Employee created"),
                data={"employee": result},
                status=HTTP_OK,
            )
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "create_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>",
        type="http", auth="none", methods=["PUT", "PATCH"], csrf=False, cors="*",
    )
    @validate_token
    def update_employee(self, employee_id, **kwargs):
        try:
            payload = self._read_json_body()
            if not payload:
                return _err(_("Empty update payload."))
            service = EmployeeService(request.env)
            result = service.update(employee_id, payload)
            return _ok(_("Employee updated"), data={"employee": result})
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "update_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>",
        type="http", auth="none", methods=["DELETE"], csrf=False, cors="*",
    )
    @validate_token
    def delete_employee(self, employee_id, **kwargs):
        try:
            service = EmployeeService(request.env)
            result = service.delete(employee_id)
            return _ok(_("Employee deleted"), data=result)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "delete_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>/archive",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def archive_employee(self, employee_id, **kwargs):
        try:
            service = EmployeeService(request.env)
            result = service.archive(employee_id)
            return _ok(_("Employee archived"), data={"employee": result})
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "archive_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>/reactivate",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def reactivate_employee(self, employee_id, **kwargs):
        try:
            service = EmployeeService(request.env)
            result = service.reactivate(employee_id)
            return _ok(_("Employee reactivated"), data={"employee": result})
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "reactivate_employee")

    @http.route(
        f"{ROUTE_PREFIX}/employees/<int:employee_id>/auto-assign-manager",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def auto_assign_manager(self, employee_id, **kwargs):
        try:
            service = EmployeeService(request.env)
            result = service.auto_assign_manager(employee_id)
            return _ok(_("Manager auto-assigned"), data=result)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "auto_assign_manager")

    # ── lookup / picker endpoints (Assigned PL / Assigned QC dropdowns) ──────
    @http.route(
        f"{ROUTE_PREFIX}/users/options",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_user_options(self, **kwargs):
        try:
            roles_param = (kwargs.get("role") or kwargs.get("roles") or "").strip()
            roles = [r.strip().lower() for r in roles_param.split(",") if r.strip()] if roles_param else None
            payload = EmployeeService(request.env).list_options(
                roles=roles,
                search=kwargs.get("search") or None,
                limit=_int_param(kwargs.get("limit"), 50),
                active=_bool_param(kwargs.get("active"), default=True),
                exclude_employee_id=_int_param(kwargs.get("exclude_employee_id")),
            )
            return _ok(_("User options"), data=payload)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_user_options")

    @http.route(
        f"{ROUTE_PREFIX}/users/ql-options",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_ql_options(self, **kwargs):
        try:
            payload = EmployeeService(request.env).list_options(
                roles=["ql"],
                search=kwargs.get("search") or None,
                limit=_int_param(kwargs.get("limit"), 50),
                active=_bool_param(kwargs.get("active"), default=True),
                exclude_employee_id=_int_param(kwargs.get("exclude_employee_id")),
            )
            return _ok(_("Assigned QL options"), data=payload)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_ql_options")

    @http.route(
        f"{ROUTE_PREFIX}/users/pl-options",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_pl_options(self, **kwargs):
        try:
            payload = EmployeeService(request.env).list_options(
                roles=["pl"],
                search=kwargs.get("search") or None,
                limit=_int_param(kwargs.get("limit"), 50),
                active=_bool_param(kwargs.get("active"), default=True),
                exclude_employee_id=_int_param(kwargs.get("exclude_employee_id")),
            )
            return _ok(_("Assigned PL options"), data=payload)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_pl_options")

    @http.route(
        f"{ROUTE_PREFIX}/users/tpm-options",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def list_tpm_options(self, **kwargs):
        try:
            payload = EmployeeService(request.env).list_options(
                roles=["tpm"],
                search=kwargs.get("search") or None,
                limit=_int_param(kwargs.get("limit"), 50),
                active=_bool_param(kwargs.get("active"), default=True),
                exclude_employee_id=_int_param(kwargs.get("exclude_employee_id")),
            )
            return _ok(_("Assigned TPM options"), data=payload)
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "list_tpm_options")

    # ── export ───────────────────────────────────────────────────────────────
    @http.route(
        f"{ROUTE_PREFIX}/employees/export/csv",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def export_csv(self, **kwargs):
        try:
            export = ExportService(request.env)
            data = export.to_csv(**self._export_filters(kwargs))
            return self._file_response(
                data, filename=f"employees_{kwargs.get('role') or 'all'}.csv",
                mimetype="text/csv",
            )
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "export_csv")

    @http.route(
        f"{ROUTE_PREFIX}/employees/export/xlsx",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def export_xlsx(self, **kwargs):
        try:
            export = ExportService(request.env)
            data = export.to_xlsx(**self._export_filters(kwargs))
            return self._file_response(
                data, filename=f"employees_{kwargs.get('role') or 'all'}.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except ImportError as exc:
            return _err(
                _("Excel export requires xlsxwriter: %s") % exc,
                status=HTTP_INTERNAL_ERROR, code=ERR_DEPENDENCY_MISSING,
            )
        except EmployeeServiceError as exc:
            return _service_error_response(exc)
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "export_xlsx")

    @http.route(
        f"{ROUTE_PREFIX}/template/csv",
        type="http", auth="none", methods=["GET"], csrf=False, cors="*",
    )
    @validate_token
    def csv_template(self, **kwargs):
        """Return a sample CSV template the caller can hand to end users."""
        try:
            body = (
                "name,employee_id,email,role,job_title,"
                "assigned_ql,assigned_pl,assigned_tpm\n"
                "Tina TPM,EMP100,tina.tpm@ethara.ai,tpm,Technical Program Manager,,,\n"
                "Mark Lead,EMP003,mark.lead@ethara.ai,pl,Project Lead,,,tina.tpm@ethara.ai\n"
                "Quinn Quality,EMP010,quinn.quality@ethara.ai,ql,Quality Lead,,mark.lead@ethara.ai,tina.tpm@ethara.ai\n"
                "Pooja Reviewer,EMP004,pooja.reviewer@ethara.ai,qr,Quality Reviewer,quinn.quality@ethara.ai,mark.lead@ethara.ai,tina.tpm@ethara.ai\n"
                "John Doe,EMP001,john.doe@ethara.ai,tasker,Annotator,quinn.quality@ethara.ai,mark.lead@ethara.ai,tina.tpm@ethara.ai\n"
            ).encode("utf-8-sig")
            return self._file_response(
                body, filename="employee_role_import_template.csv",
                mimetype="text/csv",
            )
        except Exception as exc:  # noqa: BLE001
            return self._unexpected(exc, "csv_template")

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _read_json_body():
        body = request.httprequest.data or b""
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _extract_upload():
        """Return (raw_bytes, filename) from multipart or JSON body."""
        # Multipart path
        try:
            file_storage = request.httprequest.files.get("csv_file")
        except Exception:  # noqa: BLE001
            file_storage = None
        if file_storage:
            return file_storage.read(), file_storage.filename or "upload.csv"

        # JSON path
        body = request.httprequest.data or b""
        if body:
            try:
                payload = json.loads(body)
            except (ValueError, TypeError):
                payload = {}
            csv_b64 = payload.get("csv_b64") or payload.get("csv_base64")
            filename = payload.get("filename") or "upload.csv"
            if csv_b64:
                try:
                    return base64.b64decode(csv_b64), filename
                except Exception as exc:  # noqa: BLE001
                    raise CsvParseError(
                        _("csv_b64 is not valid base64: %s") % exc
                    ) from exc
        return b"", ""

    def _fetch_session(self, token):
        if not token:
            return None
        session = (
            request.env["employee.import.session"]
            .sudo()
            .search([("session_token", "=", token)], limit=1)
        )
        if not session:
            return None
        session.assert_owner(request.env.user)
        return session

    @staticmethod
    def _export_filters(kwargs):
        return {
            "search": kwargs.get("search") or None,
            "role": (kwargs.get("role") or "").strip().lower() or None,
            "parent_id": _int_param(kwargs.get("parent_id")),
            "active": _bool_param(kwargs.get("active"), default=True),
            "include_archived": _bool_param(
                kwargs.get("include_archived"), default=False
            ),
        }

    @staticmethod
    def _file_response(data: bytes, filename: str, mimetype: str):
        headers = [
            ("Content-Type", mimetype),
            ("Content-Length", str(len(data))),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Cache-Control", "no-store"),
        ]
        return request.make_response(data, headers=headers)

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
