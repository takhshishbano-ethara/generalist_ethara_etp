"""Repository layer: pure ORM data access for hr.employee.

The repository concentrates every read/write touching ``hr.employee``
behind a small, intention-revealing API so:

* the service layer can be unit-tested without re-deriving domain logic;
* the controller layer never builds an Odoo domain by hand;
* future migrations (e.g. switching the dedup key) change in one place.

Conventions
-----------
* Every method is a thin wrapper.  No business rules live here.
* All searches are ``sudo()``-ed at the repository boundary - callers
  must have already enforced access by the time they reach this layer.
* ``with_context(active_test=False)`` is used wherever archived rows
  should still be visible (lookup-by-id, dedup detection).
"""

from typing import Iterable, Optional

from ..models.hr_employee import ROLE_HIERARCHY_FIELDS


class EmployeeRepository:
    """Repository for ``hr.employee`` operations."""

    def __init__(self, env):
        self.env = env

    # ── helpers ──────────────────────────────────────────────────────────────
    def _model(self, include_archived: bool = False):
        emp = self.env["hr.employee"].sudo()
        if include_archived:
            emp = emp.with_context(active_test=False)
        return emp

    def _users(self, include_archived: bool = False):
        users = self.env["res.users"].sudo()
        if include_archived:
            users = users.with_context(active_test=False)
        return users

    # ── single-record lookups ────────────────────────────────────────────────
    def get_by_id(self, employee_id: int, include_archived: bool = True):
        rec = self._model(include_archived).browse(int(employee_id))
        return rec if rec.exists() else self._model().browse()

    def find_by_employee_code(self, code: str, include_archived: bool = True):
        code = (code or "").strip()
        if not code:
            return self._model().browse()
        return self._model(include_archived).search(
            [("employee_code", "=ilike", code)], limit=1
        )

    def find_by_email(self, email: str, include_archived: bool = True):
        email = (email or "").strip()
        if not email:
            return self._model().browse()
        return self._model(include_archived).search(
            [("work_email", "=ilike", email)], limit=1
        )

    def find_user_by_login(self, login: str, include_archived: bool = True):
        login = (login or "").strip()
        if not login:
            return self._users().browse()
        return self._users(include_archived).search(
            [("login", "=ilike", login)], limit=1
        )

    # ── filtered queries (list / search / export) ────────────────────────────
    def build_domain(
        self,
        search: Optional[str] = None,
        role: Optional[str] = None,
        parent_id: Optional[int] = None,
        active: Optional[bool] = True,
        ids: Optional[Iterable[int]] = None,
        has_user: Optional[bool] = None,
    ):
        """Translate filter inputs into an Odoo domain.

        Each filter is independent and combines with AND.
        """
        domain = []
        if active is True:
            domain.append(("active", "=", True))
        elif active is False:
            domain.append(("active", "=", False))

        if role:
            domain.append(("role", "=", role))

        if parent_id is not None:
            domain.append(("parent_id", "=", int(parent_id)))

        if has_user is True:
            domain.append(("user_id", "!=", False))
        elif has_user is False:
            domain.append(("user_id", "=", False))

        if ids is not None:
            ids = [int(i) for i in ids]
            domain.append(("id", "in", ids))

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
        return domain

    def list_employees(
        self,
        domain,
        offset: int = 0,
        limit: int = 25,
        order: str = "name asc, id asc",
        include_archived: bool = False,
    ):
        return self._model(include_archived).search(
            domain, offset=offset, limit=limit, order=order
        )

    def count_employees(self, domain, include_archived: bool = False) -> int:
        return self._model(include_archived).search_count(domain)

    # ── mutations ────────────────────────────────────────────────────────────
    def create_employee(self, vals: dict):
        return self._model().create(vals)

    def update_employee(self, employee, vals: dict):
        return employee.sudo().write(vals)

    def reactivate(self, employee):
        if employee and not employee.active:
            employee.sudo().write({"active": True})

    def archive(self, employee):
        if employee and employee.active:
            employee.sudo().write({"active": False})

    def delete_employee(self, employee):
        """Hard-delete the employee and its linked login user.

        Mirrors ``HrEmployee.unlink`` so direct repository deletes still
        clean up the related user record.
        """
        if not employee:
            return False
        return employee.sudo().unlink()

    # ── role / hierarchy helpers ─────────────────────────────────────────────
    def find_default_manager(
        self,
        employee_role,
        role_parent_map: dict,
        extra_candidates=None,
    ):
        """Return the senior-most employee whose role satisfies the chain.

        ``role_parent_map`` is ``ROLE_DEFAULT_PARENT_ROLE`` from the
        model layer.  ``extra_candidates`` lets the caller short-circuit
        when a brand-new manager is being created in the same batch.
        """
        seen = set()
        target = role_parent_map.get(employee_role)
        while target and target not in seen:
            seen.add(target)
            if extra_candidates:
                in_batch = extra_candidates.filtered(lambda e: e.role == target)
                if in_batch:
                    return in_batch[0]
            existing = self._model().search(
                [("role", "=", target), ("active", "=", True)],
                limit=1,
            )
            if existing:
                return existing
            target = role_parent_map.get(target)
        return self._model().browse()

    def list_direct_reports(self, employee, include_archived: bool = False):
        if not employee:
            return self._model().browse()
        return self._model(include_archived).search(
            [("parent_id", "=", employee.id)]
        )

    # ── Task Forge hierarchy resolution ──────────────────────────────────────
    @staticmethod
    def _walk_ancestor_with_role(employee, target_roles):
        if not employee:
            return None
        visited = {employee.id}
        current = employee.parent_id
        while current and current.id not in visited:
            visited.add(current.id)
            if current.role in target_roles:
                return current
            current = current.parent_id
        return None

    def resolve_assigned_pl(self, employee):
        if not employee:
            return None
        fields_present = employee._fields
        if "task_forge_pl_id" in fields_present and employee.task_forge_pl_id:
            return employee.task_forge_pl_id
        if "task_forge_project_lead_id" in fields_present and employee.task_forge_project_lead_id:
            return employee.task_forge_project_lead_id
        return self._walk_ancestor_with_role(employee, ("pl",))

    def resolve_assigned_ql(self, employee):
        # QL and QR are a single quality tier: honour either stored field and
        # walk the chain for the nearest manager holding *either* role.
        if not employee:
            return None
        fields_present = employee._fields
        if "task_forge_qr_id" in fields_present and employee.task_forge_qr_id:
            return employee.task_forge_qr_id
        if "task_forge_ql_id" in fields_present and employee.task_forge_ql_id:
            return employee.task_forge_ql_id
        return self._walk_ancestor_with_role(employee, ("ql", "qr"))

    def resolve_assigned_tpm(self, employee):
        if not employee:
            return None
        fields_present = employee._fields
        if "task_forge_tpm_id" in fields_present and employee.task_forge_tpm_id:
            return employee.task_forge_tpm_id
        return self._walk_ancestor_with_role(employee, ("tpm",))

    def propagate_task_forge_chain(self, employee):
        if not employee:
            return
        fields_present = employee._fields
        applicable = set(ROLE_HIERARCHY_FIELDS.get(employee.role or "", ()))
        vals = {}

        for role_key, field_name in (
            ("ql", "task_forge_ql_id"),
            ("pl", "task_forge_pl_id"),
            ("tpm", "task_forge_tpm_id"),
        ):
            if field_name not in fields_present:
                continue
            if role_key in applicable:
                ancestor = self._walk_ancestor_with_role(employee, (role_key,))
                vals[field_name] = ancestor.id if ancestor else False
            else:
                vals[field_name] = False

        if "task_forge_qr_id" in fields_present:
            if employee.role in ("tasker",):
                qr = self._walk_ancestor_with_role(employee, ("qr",))
                vals["task_forge_qr_id"] = qr.id if qr else False
            else:
                vals["task_forge_qr_id"] = False

        if vals:
            employee.sudo().write(vals)
