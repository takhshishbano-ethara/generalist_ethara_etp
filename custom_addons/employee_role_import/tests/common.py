"""Shared fixtures for the employee_role_import test suite.

Provides:
* :class:`EmployeeRoleImportCase` - TransactionCase base with HR user, HR
  manager, a non-HR user, and helper factories for employees / CSV
  payloads / sessions.
* :func:`build_csv` - tiny helper that builds an in-memory CSV body with
  the canonical column set used by the importer.
"""

import base64
import io
import csv

from odoo.tests.common import TransactionCase, tagged


def build_csv(rows, headers=("name", "employee_id", "email", "role", "reports_to")):
    """Build a small CSV as bytes from a list of dict rows."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(headers))
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8")


@tagged("post_install", "-at_install", "employee_role_import")
class EmployeeRoleImportCase(TransactionCase):
    """Base test case providing HR users and reusable factories."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base_group = cls.env.ref("base.group_user")
        hr_user = cls.env.ref("hr.group_hr_user")
        hr_manager = cls.env.ref("hr.group_hr_manager")

        cls.user_hr = Users.create({
            "name": "HR User",
            "login": "eri_hr_user",
            "email": "eri_hr_user@example.com",
            "group_ids": [(6, 0, [base_group.id, hr_user.id])],
        })
        cls.user_hr_manager = Users.create({
            "name": "HR Manager",
            "login": "eri_hr_manager",
            "email": "eri_hr_manager@example.com",
            "group_ids": [(6, 0, [base_group.id, hr_manager.id])],
        })
        cls.user_plain = Users.create({
            "name": "Plain User",
            "login": "eri_plain_user",
            "email": "eri_plain@example.com",
            "group_ids": [(6, 0, [base_group.id])],
        })

    # ── helpers ──────────────────────────────────────────────────────────────
    def _make_employee(self, code="EMP100", name="Alice", email=None, role=None,
                      parent=None, user=None, active=True):
        email = email or f"{code.lower()}@example.com"
        vals = {
            "name": name,
            "employee_code": code,
            "work_email": email,
            "active": active,
        }
        if user:
            vals["user_id"] = user.id
        if parent:
            vals["parent_id"] = parent.id
        emp = self.env["hr.employee"].sudo().create(vals)
        if role:
            try:
                emp.sudo().role = role
            except Exception:
                # role assignment without a user falls through - tests can
                # opt into that path explicitly via _make_user_for_employee.
                pass
        return emp

    def _make_user_for(self, name, login, password="Test@1234"):
        return self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": name,
            "login": login,
            "email": login,
            "password": password,
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })

    def _make_session(self, rows, default_role=None, create_user=True):
        body = build_csv(rows)
        from ..services.csv_import_service import CsvImportService
        service = CsvImportService(self.env)
        session = service.create_session(
            raw_bytes=body,
            filename="test.csv",
            default_role=default_role or "",
            create_user=create_user,
            default_password="Test@1234",
        )
        return session, service
