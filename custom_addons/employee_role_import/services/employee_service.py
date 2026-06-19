"""Employee service: CRUD + hierarchy operations consumed by the REST API.

Layer responsibilities:

* Input validation (presence, types, uniqueness, email shape, role).
* Cross-field business rules (manager seniority, archived re-use).
* Translation between API DTOs (dict-in / dict-out) and ORM records.

Anything that touches the database goes through
:class:`EmployeeRepository` so this module stays test-friendly: the
service can be unit-tested with a stub repository if desired.
"""

import logging
import re
from typing import Optional

from odoo import _
from odoo.exceptions import AccessError, UserError, ValidationError

from ..models.hr_employee import (
    ROLE_DEFAULT_PARENT_ROLE,
    ROLE_GROUP_MAP,
    ROLE_HIERARCHY_FIELDS,
    ROLE_LEVEL,
    ROLE_SELECTION,
)
from .employee_repository import EmployeeRepository
from .response_codes import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
)

_logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMPLOYEE_CODE_RE = re.compile(r"^[A-Z]{2,6}\d+$")
EMPLOYEE_CODE_HINT = (
    "Employee ID must be 2-6 uppercase letters followed by digits "
    "(e.g. GRT1137 or GRTP6789)."
)
ROLE_LABELS = dict(ROLE_SELECTION)


class EmployeeServiceError(UserError):
    """Domain-level error from the employee service.

    Carries a stable machine-readable ``code`` so the controller layer
    can convert it into the right HTTP status.
    """

    def __init__(self, message, code="validation_error", http_status=400, details=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details or {}


class EmployeeService:
    """Business orchestration for employee records."""

    def __init__(self, env):
        self.env = env
        self.repo = EmployeeRepository(env)

    # ── Read ────────────────────────────────────────────────────────────────
    def get(self, employee_id: int, include_archived: bool = True) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=include_archived)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        return self.serialize(emp)

    def list(
        self,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
        search: Optional[str] = None,
        role: Optional[str] = None,
        parent_id: Optional[int] = None,
        active: Optional[bool] = True,
        has_user: Optional[bool] = None,
        order: str = "name asc, id asc",
        include_archived: bool = False,
    ) -> dict:
        page, limit, offset = self._normalize_paging(page, limit)
        if role and role not in ROLE_GROUP_MAP:
            raise EmployeeServiceError(
                _("Unknown role '%s'.") % role,
                code="unknown_role",
            )
        domain = self.repo.build_domain(
            search=search,
            role=role,
            parent_id=parent_id,
            active=None if include_archived else active,
            has_user=has_user,
        )
        total = self.repo.count_employees(domain, include_archived=include_archived)
        employees = self.repo.list_employees(
            domain, offset=offset, limit=limit, order=order,
            include_archived=include_archived,
        )
        return {
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

    # ── Create ──────────────────────────────────────────────────────────────
    def create(self, payload: dict) -> dict:
        self._validate_payload(payload, partial=False)
        code = (payload.get("employee_id") or "").strip()
        email = (payload.get("email") or "").strip().lower()
        role_key = (payload.get("role") or "").strip().lower() or None

        if self.repo.find_by_employee_code(code, include_archived=True):
            raise EmployeeServiceError(
                _("Employee ID '%s' already exists.") % code,
                code="duplicate_employee",
                http_status=409,
            )
        if self.repo.find_by_email(email, include_archived=True):
            raise EmployeeServiceError(
                _("Email '%s' is already in use.") % email,
                code="duplicate_employee",
                http_status=409,
            )

        vals = {
            "name": payload["name"].strip(),
            "employee_code": code,
            "work_email": email,
        }
        job_title = (payload.get("job_title") or "").strip()
        if job_title:
            vals["job_title"] = job_title
        parent_id = self._resolve_parent(
            payload.get("reports_to_employee_id"), role_key,
        )
        if parent_id:
            vals["parent_id"] = parent_id

        try:
            emp = self.repo.create_employee(vals)
        except (ValidationError, AccessError) as exc:
            raise EmployeeServiceError(str(exc)) from exc

        if role_key:
            try:
                emp.sudo().role = role_key
            except UserError as exc:
                _logger.info(
                    "Role assignment skipped for new employee #%s: %s",
                    emp.id, exc,
                )

        direct_hierarchy_vals = self._collect_direct_hierarchy_vals(emp, payload)
        if direct_hierarchy_vals:
            self.repo.update_employee(emp, direct_hierarchy_vals)
        else:
            self.repo.propagate_task_forge_chain(emp)
        return self.serialize(emp)

    # ── Update ──────────────────────────────────────────────────────────────
    def update(self, employee_id: int, payload: dict) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=True)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        self._validate_payload(payload, partial=True)

        vals = {}
        if "name" in payload:
            name = (payload["name"] or "").strip()
            if not name:
                raise EmployeeServiceError(_("Name cannot be empty."))
            vals["name"] = name

        if "email" in payload:
            email = (payload["email"] or "").strip().lower()
            if not email:
                raise EmployeeServiceError(_("Email cannot be empty."))
            if not EMAIL_RE.match(email):
                raise EmployeeServiceError(_("'%s' is not a valid email.") % email)
            other = self.repo.find_by_email(email, include_archived=True)
            if other and other.id != emp.id:
                raise EmployeeServiceError(
                    _("Email '%s' is already in use by employee #%d.")
                    % (email, other.id),
                    code="duplicate_employee",
                    http_status=409,
                )
            vals["work_email"] = email

        if "employee_id" in payload:
            code = str(payload["employee_id"] or "").strip()
            if not code:
                raise EmployeeServiceError(_("Employee ID cannot be empty."))
            other = self.repo.find_by_employee_code(code, include_archived=True)
            if other and other.id != emp.id:
                raise EmployeeServiceError(
                    _("Employee ID '%s' is already in use by employee #%d.")
                    % (code, other.id),
                    code="duplicate_employee",
                    http_status=409,
                )
            vals["employee_code"] = code

        if "reports_to_employee_id" in payload:
            parent_id = payload["reports_to_employee_id"]
            if parent_id in (None, "", False):
                vals["parent_id"] = False
            else:
                parent = self.repo.get_by_id(parent_id)
                if not parent:
                    raise EmployeeServiceError(
                        _("Manager employee #%s not found.") % parent_id,
                        code="not_found",
                        http_status=404,
                    )
                # Hierarchy seniority check happens via model constrains.
                vals["parent_id"] = parent.id

        direct_hierarchy_vals = self._collect_direct_hierarchy_vals(emp, payload)
        vals.update(direct_hierarchy_vals)
        if "job_title" in payload and "job_title" in emp._fields:
            job_title = (payload.get("job_title") or "").strip()
            vals["job_title"] = job_title or False

        if vals:
            try:
                self.repo.update_employee(emp, vals)
            except (ValidationError, AccessError) as exc:
                raise EmployeeServiceError(str(exc)) from exc

        if "role" in payload:
            role_key = (payload["role"] or "").strip().lower() or False
            if role_key and role_key not in ROLE_GROUP_MAP:
                raise EmployeeServiceError(
                    _("Unknown role '%s'.") % role_key,
                    code="unknown_role",
                )
            try:
                emp.sudo().role = role_key
            except UserError as exc:
                raise EmployeeServiceError(str(exc)) from exc

        # Skip the chain walk when the caller already supplied authoritative
        # task_forge_pl_id / task_forge_qr_id; otherwise the walk would
        # overwrite them based on the parent_id chain.
        if "reports_to_employee_id" in payload or "role" in payload:
            if not direct_hierarchy_vals:
                self.repo.propagate_task_forge_chain(emp)

        return self.serialize(emp)

    def _collect_direct_hierarchy_vals(self, emp, payload):
        out = {}
        fields_present = emp._fields
        for payload_key, field in (
            ("assigned_ql_employee_id", "task_forge_ql_id"),
            ("assigned_pl_employee_id", "task_forge_pl_id"),
            ("assigned_tpm_employee_id", "task_forge_tpm_id"),
            ("assigned_qc_employee_id", "task_forge_qr_id"),
        ):
            if payload_key not in payload or field not in fields_present:
                continue
            target_id = payload[payload_key]
            if target_id in (None, "", False, 0):
                out[field] = False
                continue
            target = self.repo.get_by_id(target_id)
            if not target:
                raise EmployeeServiceError(
                    _("%s #%s not found.") % (payload_key, target_id),
                    code="not_found",
                    http_status=404,
                )
            out[field] = target.id
        return out

    # ── Delete ──────────────────────────────────────────────────────────────
    def delete(self, employee_id: int) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=True)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        reports = self.repo.list_direct_reports(emp, include_archived=True)
        # Detach the children's parent_id pointer so we don't leak FKs
        # pointing at a freshly-deleted record.  The model's unlink also
        # cleans up the linked res.users record.
        if reports:
            reports.sudo().write({"parent_id": False})
            for child in reports:
                self.repo.propagate_task_forge_chain(child)

        deleted_id = emp.id
        deleted_name = emp.name
        deleted_code = emp.employee_code
        deleted_email = emp.work_email
        had_user = bool(emp.user_id)

        self.repo.delete_employee(emp)
        return {
            "deleted": True,
            "employee_id": deleted_id,
            "name": deleted_name,
            "employee_code": deleted_code,
            "email": deleted_email,
            "linked_user_removed": had_user,
            "direct_reports_detached": len(reports),
        }

    def archive(self, employee_id: int) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=True)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        self.repo.archive(emp)
        return self.serialize(emp)

    def reactivate(self, employee_id: int) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=True)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        self.repo.reactivate(emp)
        return self.serialize(emp)

    # ── Lookups (lightweight options for dropdowns / pickers) ───────────────
    def list_options(
        self,
        roles=None,
        search: Optional[str] = None,
        limit: int = 50,
        active: bool = True,
        exclude_employee_id: Optional[int] = None,
    ) -> dict:
        """Return a lightweight selectable list for dropdown / typeahead UIs.

        Designed for the "Assigned PL" / "Assigned QC" pickers and any
        future single-purpose selectors.  Payload deliberately omits
        timestamps, parent objects, and pagination metadata so the Flutter
        client can render long lists cheaply.

        ``roles`` may be a single role key or an iterable of keys.  The
        empty / ``None`` value returns ALL active employees (subject to
        ``limit``) so the same endpoint can power a generic employee
        picker.
        """
        try:
            limit = int(limit or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        role_filter = []
        if isinstance(roles, str):
            roles = [roles]
        if roles:
            for raw in roles:
                if raw is None:
                    continue
                key = str(raw).strip().lower()
                if not key:
                    continue
                if key not in ROLE_GROUP_MAP:
                    raise EmployeeServiceError(
                        _("Unknown role '%s'.") % key,
                        code="unknown_role",
                    )
                if key not in role_filter:
                    role_filter.append(key)

        domain = [("active", "=", True)] if active else []
        if role_filter:
            if len(role_filter) == 1:
                domain.append(("role", "=", role_filter[0]))
            else:
                domain.append(("role", "in", role_filter))
        if exclude_employee_id:
            try:
                domain.append(("id", "!=", int(exclude_employee_id)))
            except (TypeError, ValueError):
                pass
        if search:
            term = search.strip()
            if term:
                domain += [
                    "|", "|", "|",
                    ("name", "ilike", term),
                    ("employee_code", "ilike", term),
                    ("work_email", "ilike", term),
                    ("parent_id.name", "ilike", term),
                ]

        employees = self.repo.list_employees(
            domain, offset=0, limit=limit,
            order="name asc, id asc",
            include_archived=not active,
        )

        return {
            "data": [
                {
                    "id": emp.id,
                    "name": emp.name or "",
                    "employee_code": emp.employee_code or "",
                    "email": emp.work_email or "",
                    "role": emp.role or "",
                    "role_label": ROLE_LABELS.get(emp.role, "") if emp.role else "",
                }
                for emp in employees
            ],
            "count": len(employees),
            "role_filter": role_filter,
            "limit": limit,
            "search": search or None,
        }

    # ── Hierarchy ───────────────────────────────────────────────────────────
    def role_hierarchy(self) -> dict:
        chains = []
        seen = set()
        for role_key in ROLE_LEVEL:
            if role_key in seen:
                continue
            chain = []
            cur = role_key
            visited = set()
            while cur and cur not in visited:
                visited.add(cur)
                chain.append({
                    "role": cur,
                    "label": ROLE_LABELS.get(cur, cur),
                    "level": ROLE_LEVEL.get(cur, 0),
                })
                cur = ROLE_DEFAULT_PARENT_ROLE.get(cur)
            chains.append(chain)
            seen.update(visited)
        # Sort so the lowest-level chain (tasker→…) appears first.
        chains.sort(key=lambda c: c[0]["level"])
        return {
            "roles": [
                {
                    "key": k,
                    "label": ROLE_LABELS.get(k, k),
                    "level": ROLE_LEVEL.get(k, 0),
                    "default_parent_role": ROLE_DEFAULT_PARENT_ROLE.get(k),
                }
                for k in ROLE_GROUP_MAP
            ],
            "chains": chains,
            "default_parent_map": dict(ROLE_DEFAULT_PARENT_ROLE),
        }

    def auto_assign_manager(self, employee_id: int) -> dict:
        emp = self.repo.get_by_id(employee_id, include_archived=False)
        if not emp:
            raise EmployeeServiceError(
                _("Employee #%s not found.") % employee_id,
                code="not_found",
                http_status=404,
            )
        if not emp.role:
            raise EmployeeServiceError(
                _("Employee #%s has no role - cannot derive a default manager.")
                % employee_id,
                code="validation_error",
            )
        manager = self.repo.find_default_manager(
            emp.role, ROLE_DEFAULT_PARENT_ROLE,
        )
        if not manager:
            raise EmployeeServiceError(
                _("No senior employee found to assign as manager for role '%s'.")
                % emp.role,
                code="not_found",
                http_status=404,
            )
        emp.sudo().write({"parent_id": manager.id})
        self.repo.propagate_task_forge_chain(emp)
        return {
            "employee_id": emp.id,
            "manager_id": manager.id,
            "manager_name": manager.name,
            "manager_role": manager.role,
        }

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _validate_payload(self, payload: dict, partial: bool):
        if not isinstance(payload, dict):
            raise EmployeeServiceError(_("Request body must be a JSON object."))
        required = ("name", "employee_id", "email")
        if not partial:
            missing = [f for f in required if not (payload.get(f) or "").strip()]
            if missing:
                raise EmployeeServiceError(
                    _("Missing required field(s): %s") % ", ".join(missing),
                    details={"missing_fields": missing},
                )
            email = (payload["email"] or "").strip().lower()
            if not EMAIL_RE.match(email):
                raise EmployeeServiceError(_("'%s' is not a valid email.") % email)
        if "employee_id" in payload:
            code = str(payload["employee_id"] or "").strip()
            if code and not EMPLOYEE_CODE_RE.match(code):
                raise EmployeeServiceError(
                    _("Employee ID '%s' has an invalid format.  %s") % (code, EMPLOYEE_CODE_HINT),
                    details={"field": "employee_id", "pattern": EMPLOYEE_CODE_RE.pattern},
                )
        role = (payload.get("role") or "").strip().lower()
        if role and role not in ROLE_GROUP_MAP:
            raise EmployeeServiceError(
                _("Unknown role '%s'.  Allowed: %s")
                % (role, ", ".join(sorted(ROLE_GROUP_MAP.keys()))),
                code="unknown_role",
            )

    def _resolve_parent(self, reports_to_id, role_key) -> Optional[int]:
        if reports_to_id:
            parent = self.repo.get_by_id(reports_to_id)
            if not parent:
                raise EmployeeServiceError(
                    _("Manager employee #%s not found.") % reports_to_id,
                    code="not_found",
                    http_status=404,
                )
            return parent.id
        if role_key:
            manager = self.repo.find_default_manager(
                role_key, ROLE_DEFAULT_PARENT_ROLE,
            )
            return manager.id if manager else None
        return None

    def _normalize_paging(self, page, limit):
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = int(limit or DEFAULT_PAGE_SIZE)
        except (TypeError, ValueError):
            limit = DEFAULT_PAGE_SIZE
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = (page - 1) * limit
        return page, limit, offset

    def serialize(self, emp) -> dict:
        if not emp:
            return {}
        applicable = ROLE_HIERARCHY_FIELDS.get(emp.role or "", ())
        assigned_ql = self.repo.resolve_assigned_ql(emp) if "ql" in applicable else None
        assigned_pl = self.repo.resolve_assigned_pl(emp) if "pl" in applicable else None
        assigned_tpm = self.repo.resolve_assigned_tpm(emp) if "tpm" in applicable else None
        return {
            "id": emp.id,
            "employee_id": emp.employee_code or "",
            "name": emp.name or "",
            "email": emp.work_email or "",
            "role": emp.role or "",
            "role_label": ROLE_LABELS.get(emp.role, "") if emp.role else "",
            "job_title": emp.job_title or "",
            "hierarchy_fields": list(applicable),
            "reports_to": {
                "id": emp.parent_id.id,
                "name": emp.parent_id.name or "",
                "role": emp.parent_id.role or "",
            } if emp.parent_id else None,
            "assigned_ql": self._short_employee(assigned_ql),
            "assigned_pl": self._short_employee(assigned_pl),
            "assigned_tpm": self._short_employee(assigned_tpm),
            "assigned_ql_id": assigned_ql.id if assigned_ql else None,
            "assigned_ql_name": assigned_ql.name if assigned_ql else None,
            "assigned_pl_id": assigned_pl.id if assigned_pl else None,
            "assigned_pl_name": assigned_pl.name if assigned_pl else None,
            "assigned_tpm_id": assigned_tpm.id if assigned_tpm else None,
            "assigned_tpm_name": assigned_tpm.name if assigned_tpm else None,
            "user_id": emp.user_id.id if emp.user_id else None,
            "user_login": emp.user_id.login if emp.user_id else None,
            "active": bool(emp.active),
            "status": "active" if emp.active else "archived",
            "created_at": emp.create_date.isoformat() if emp.create_date else None,
            "updated_at": emp.write_date.isoformat() if emp.write_date else None,
        }

    @staticmethod
    def _short_employee(emp) -> Optional[dict]:
        if not emp:
            return None
        return {
            "id": emp.id,
            "name": emp.name or "",
            "role": emp.role or "",
            "role_label": ROLE_LABELS.get(emp.role, "") if emp.role else "",
        }
