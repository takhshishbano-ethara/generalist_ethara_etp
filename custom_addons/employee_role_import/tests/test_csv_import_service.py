"""Unit tests for CsvImportService.

Coverage:
* Header validation (missing required columns -> CsvParseError).
* Row-level validation (missing fields, invalid email, unknown role).
* Duplicate detection within the CSV and against the DB.
* Commit creates new rows, updates existing rows by employee_id,
  falls back to email when employee_id is absent in the DB.
* Auto-assignment of reports_to based on role chain.
* Re-committing an already-imported session returns the cached summary.
"""

from odoo.exceptions import UserError

from .common import EmployeeRoleImportCase, build_csv
from ..services.csv_import_service import CsvImportService, CsvParseError


class TestCsvHeaderValidation(EmployeeRoleImportCase):

    def test_missing_required_columns_raises(self):
        body = b"name,email\nAlice,alice@example.com\n"
        service = CsvImportService(self.env)
        session = service.create_session(raw_bytes=body, filename="bad.csv")
        with self.assertRaises(CsvParseError):
            service.build_preview(session)

    def test_empty_file_raises(self):
        service = CsvImportService(self.env)
        with self.assertRaises(CsvParseError):
            service.create_session(raw_bytes=b"", filename="empty.csv")


class TestRowValidation(EmployeeRoleImportCase):

    def test_missing_email_marked_invalid(self):
        session, service = self._make_session([
            {"name": "Alice", "employee_id": "E1", "email": "", "role": "tasker"},
        ])
        preview = service.build_preview(session)
        self.assertEqual(preview["totals"]["invalid"], 1)
        row = preview["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("email" in i for i in row["issues"]))

    def test_invalid_email_marked_invalid(self):
        session, service = self._make_session([
            {"name": "Alice", "employee_id": "E1", "email": "not-an-email",
             "role": "tasker"},
        ])
        preview = service.build_preview(session)
        row = preview["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("valid address" in i for i in row["issues"]))

    def test_unknown_role_marked_invalid(self):
        session, service = self._make_session([
            {"name": "Alice", "employee_id": "E1",
             "email": "alice@example.com", "role": "ceo"},
        ])
        preview = service.build_preview(session)
        row = preview["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("unknown role" in i for i in row["issues"]))

    def test_missing_role_uses_default_role(self):
        session, service = self._make_session(
            [
                {"name": "Alice", "employee_id": "E1",
                 "email": "alice@example.com", "role": ""},
            ],
            default_role="tasker",
        )
        preview = service.build_preview(session)
        self.assertEqual(preview["rows"][0]["role"], "tasker")
        self.assertEqual(preview["rows"][0]["status"], "valid")


class TestDuplicateDetection(EmployeeRoleImportCase):

    def test_duplicate_employee_id_within_csv(self):
        session, service = self._make_session([
            {"name": "Alice", "employee_id": "E1",
             "email": "alice@example.com", "role": "tasker"},
            {"name": "Bob", "employee_id": "E1",
             "email": "bob@example.com", "role": "tasker"},
        ])
        preview = service.build_preview(session)
        statuses = [r["status"] for r in preview["rows"]]
        self.assertIn("duplicate_in_file", statuses)
        self.assertEqual(preview["totals"]["duplicate_in_file"], 1)

    def test_existing_employee_id_routes_to_update(self):
        existing_user = self._make_user_for("Original", "orig@example.com")
        self._make_employee(
            code="E2", name="Original", email="orig@example.com", user=existing_user,
        )
        session, service = self._make_session([
            {"name": "Renamed", "employee_id": "E2",
             "email": "renamed@example.com", "role": "tasker"},
        ])
        preview = service.build_preview(session)
        row = preview["rows"][0]
        self.assertEqual(row["status"], "exists")
        self.assertIsNotNone(row["existing_employee_id"])

    def test_existing_email_routes_to_update_when_no_code_match(self):
        self._make_employee(code="E3", name="Match", email="match@example.com")
        session, service = self._make_session([
            {"name": "Match Updated", "employee_id": "DIFFERENT",
             "email": "match@example.com", "role": "tasker"},
        ])
        preview = service.build_preview(session)
        self.assertEqual(preview["rows"][0]["status"], "exists")


class TestCommit(EmployeeRoleImportCase):

    def test_commit_creates_new_employees(self):
        session, service = self._make_session([
            {"name": "Alice", "employee_id": "C1",
             "email": "c1@example.com", "role": "tasker"},
            {"name": "Bob", "employee_id": "C2",
             "email": "c2@example.com", "role": "tasker"},
        ])
        service.build_preview(session)
        summary = service.commit(session)
        self.assertEqual(summary["results"]["imported"], 2)
        self.assertEqual(summary["results"]["updated"], 0)
        self.assertEqual(summary["results"]["failed"], 0)

        emps = self.env["hr.employee"].sudo().search([
            ("employee_code", "in", ["C1", "C2"]),
        ])
        self.assertEqual(len(emps), 2)

    def test_commit_updates_existing_by_code(self):
        u = self._make_user_for("Orig", "orig2@example.com")
        self._make_employee(code="U1", name="Orig",
                           email="orig2@example.com", user=u)
        session, service = self._make_session([
            {"name": "Renamed", "employee_id": "U1",
             "email": "renamed2@example.com", "role": "tasker"},
        ])
        service.build_preview(session)
        summary = service.commit(session)
        self.assertEqual(summary["results"]["updated"], 1)
        self.assertEqual(summary["results"]["imported"], 0)
        emp = self.env["hr.employee"].sudo().search(
            [("employee_code", "=", "U1")], limit=1,
        )
        self.assertEqual(emp.name, "Renamed")
        self.assertEqual(emp.work_email, "renamed2@example.com")

    def test_commit_is_idempotent(self):
        session, service = self._make_session([
            {"name": "Ida", "employee_id": "I1",
             "email": "ida@example.com", "role": "tasker"},
        ])
        service.build_preview(session)
        first = service.commit(session)
        second = service.commit(session)
        self.assertEqual(first["results"], second["results"])

    def test_commit_skips_invalid_rows_but_processes_valid(self):
        session, service = self._make_session([
            {"name": "Good", "employee_id": "G1",
             "email": "good@example.com", "role": "tasker"},
            {"name": "Bad", "employee_id": "G2",
             "email": "not-email", "role": "tasker"},
        ])
        service.build_preview(session)
        summary = service.commit(session)
        self.assertEqual(summary["results"]["imported"], 1)
        self.assertEqual(summary["results"]["failed"], 1)

    def test_reports_to_auto_assignment_picks_senior_manager(self):
        # Create a PL manager in the DB, then import a ql under it.
        pl_user = self._make_user_for("Boss", "boss@example.com")
        boss = self._make_employee(code="PL1", name="Boss",
                                  email="boss@example.com", user=pl_user)
        # set role via sudo write to bypass the strict role-required path
        boss.sudo().role = "pl"
        session, service = self._make_session([
            {"name": "QL Person", "employee_id": "QL1",
             "email": "ql1@example.com", "role": "ql"},
        ])
        service.build_preview(session)
        service.commit(session)
        new_emp = self.env["hr.employee"].sudo().search(
            [("employee_code", "=", "QL1")], limit=1,
        )
        self.assertEqual(new_emp.parent_id, boss)
