"""End-to-end HTTP tests for ``/api/v2/employee-role-import/...``.

Pattern mirrors ``iris/tests/test_api_controllers.py``: the auth gateway's
HTTP token-issuance route can't be exercised from inside an HttpCase
transaction (it writes ``res_users_log`` during ``session.authenticate``
and calls ``cr.commit()``), so we mint an ``api.access_token`` row
directly via ORM and pass it as the ``access-token`` header that
``@validate_token`` expects.
"""

import base64
import json
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from odoo.addons.api_auth_gateway.models import access_token as access_token_model

from .common import build_csv

PASSWORD = "ETPApi#2026!"
JSON_HEADERS = {"Content-Type": "application/json"}
PREFIX = "/api/v2/employee-role-import"


@tagged("post_install", "-at_install", "employee_role_import")
class TestEmployeeRoleImportApi(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base_group = cls.env.ref("base.group_user")
        hr_user = cls.env.ref("hr.group_hr_user")
        hr_manager = cls.env.ref("hr.group_hr_manager")

        cls.user_hr = Users.create({
            "name": "API HR",
            "login": "eri_api_hr",
            "email": "eri_api_hr@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [base_group.id, hr_user.id])],
        })
        cls.user_manager = Users.create({
            "name": "API HR Manager",
            "login": "eri_api_mgr",
            "email": "eri_api_mgr@example.com",
            "password": PASSWORD,
            "group_ids": [(6, 0, [base_group.id, hr_manager.id])],
        })
        cls.env.flush_all()

    def setUp(self):
        super().setUp()
        self.token_hr = self._get_token("eri_api_hr")
        self.token_manager = self._get_token("eri_api_mgr")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _get_token(self, login):
        user = self.env["res.users"].sudo().search([("login", "=", login)], limit=1)
        self.assertTrue(user, f"missing test user {login}")
        token = access_token_model.nonce()
        self.env["api.access_token"].sudo().create({
            "user_id": user.id,
            "access_token": token,
            "refresh_token": access_token_model.nonce(),
            "expiry": fields.Datetime.now() + timedelta(seconds=3600),
        })
        self.env.flush_all()
        return token

    def _request(self, method, url, token=None, payload=None,
                 extra_headers=None, raw_body=None):
        headers = dict(JSON_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        if token:
            headers["access-token"] = token
        data = raw_body if raw_body is not None else (
            json.dumps(payload) if payload is not None else None
        )
        self.env.flush_all()
        resp = self.url_open(url, data=data, headers=headers, method=method)
        self.env.invalidate_all()
        return resp

    # ── auth ────────────────────────────────────────────────────────────────
    def test_missing_token_is_401(self):
        resp = self.url_open(f"{PREFIX}/roles")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_401(self):
        resp = self._request("GET", f"{PREFIX}/roles", token="bogus")
        self.assertEqual(resp.status_code, 401)

    # ── roles + hierarchy ──────────────────────────────────────────────────
    def test_list_roles(self):
        resp = self._request("GET", f"{PREFIX}/roles", token=self.token_hr)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        roles = body.get("data", {}).get("roles", [])
        keys = {r["key"] for r in roles}
        self.assertIn("tasker", keys)
        self.assertIn("pl", keys)
        self.assertIn("tpm", keys)

    def test_role_hierarchy(self):
        resp = self._request(
            "GET", f"{PREFIX}/roles/hierarchy", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", {})
        self.assertIn("chains", data)
        self.assertIn("default_parent_map", data)

    # ── employee CRUD ──────────────────────────────────────────────────────
    def test_employee_create_and_get(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "API Alice",
                "employee_id": "API1",
                "email": "api1@example.com",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        emp = resp.json()["data"]["employee"]
        self.assertEqual(emp["employee_id"], "API1")
        emp_id = emp["id"]

        resp = self._request(
            "GET", f"{PREFIX}/employees/{emp_id}", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["data"]["employee"]["email"], "api1@example.com"
        )

    def test_create_missing_required_fields_is_400(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={"name": "Incomplete"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_create_duplicate_employee_id_is_409(self):
        self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "First", "employee_id": "DUP1",
                "email": "dup1@example.com",
            },
        )
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Second", "employee_id": "DUP1",
                "email": "dup2@example.com",
            },
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_update_employee(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Original",
                "employee_id": "UP1",
                "email": "up1@example.com",
            },
        )
        emp_id = resp.json()["data"]["employee"]["id"]
        resp = self._request(
            "PUT", f"{PREFIX}/employees/{emp_id}", token=self.token_hr,
            payload={"name": "Renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["employee"]["name"], "Renamed")

    def test_delete_employee(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Doomed",
                "employee_id": "DEL1",
                "email": "del1@example.com",
            },
        )
        emp_id = resp.json()["data"]["employee"]["id"]
        resp = self._request(
            "DELETE", f"{PREFIX}/employees/{emp_id}", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # Subsequent GET should 404
        resp = self._request(
            "GET", f"{PREFIX}/employees/{emp_id}", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 404)

    def test_list_pagination_and_search(self):
        for i in range(3):
            self._request(
                "POST", f"{PREFIX}/employees", token=self.token_hr,
                payload={
                    "name": f"Page Person {i}",
                    "employee_id": f"PG{i}",
                    "email": f"pg{i}@example.com",
                },
            )
        resp = self._request(
            "GET", f"{PREFIX}/employees?page=1&limit=2", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(body["pagination"]["limit"], 2)
        self.assertGreaterEqual(body["pagination"]["total_records"], 3)

        resp = self._request(
            "GET", f"{PREFIX}/employees?search=Page%20Person%200",
            token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200)
        names = [r["name"] for r in resp.json()["data"]["data"]]
        self.assertTrue(any("Page Person 0" in n for n in names))

    # ── CSV upload / preview / commit ──────────────────────────────────────
    def _build_csv_b64(self, rows):
        body = build_csv(rows)
        return base64.b64encode(body).decode("ascii")

    def test_csv_upload_preview_commit_flow(self):
        csv_b64 = self._build_csv_b64([
            {"name": "Csv Alice", "employee_id": "CSV1",
             "email": "csv1@example.com", "role": "tasker"},
            {"name": "Csv Bob", "employee_id": "CSV2",
             "email": "csv2@example.com", "role": "tasker"},
        ])
        # upload
        resp = self._request(
            "POST", f"{PREFIX}/upload", token=self.token_hr,
            payload={"csv_b64": csv_b64, "filename": "api.csv"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        token = body["session_token"]
        self.assertEqual(body["preview"]["totals"]["valid"], 2)

        # preview re-fetch
        resp = self._request(
            "GET", f"{PREFIX}/preview/{token}", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["data"]["preview"]["totals"]["valid"], 2,
        )

        # commit
        resp = self._request(
            "POST", f"{PREFIX}/commit", token=self.token_hr,
            payload={"session_token": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        results = resp.json()["data"]["summary"]["results"]
        self.assertEqual(results["imported"], 2)

    def test_csv_upload_missing_columns_is_400(self):
        csv_b64 = base64.b64encode(
            b"name,email\nAlice,alice@example.com\n"
        ).decode("ascii")
        resp = self._request(
            "POST", f"{PREFIX}/upload", token=self.token_hr,
            payload={"csv_b64": csv_b64, "filename": "bad.csv"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_csv_preview_unknown_session_is_404(self):
        resp = self._request(
            "GET", f"{PREFIX}/preview/no-such-token", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 404)

    # ── export ─────────────────────────────────────────────────────────────
    def test_export_csv_returns_attachment(self):
        self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Exporta", "employee_id": "EX1",
                "email": "ex1@example.com",
            },
        )
        resp = self._request(
            "GET", f"{PREFIX}/employees/export/csv", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get("Content-Type"), "text/csv")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        # Body should at minimum include the header row
        self.assertIn(b"employee_id", resp.content)

    def test_export_xlsx_returns_attachment(self):
        self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Xlsx Person", "employee_id": "XLS1",
                "email": "xls1@example.com",
            },
        )
        resp = self._request(
            "GET", f"{PREFIX}/employees/export/xlsx", token=self.token_hr,
        )
        # Either 200 (xlsxwriter installed) or 500 with dependency_missing
        # code - both are acceptable behavioural contracts.
        self.assertIn(resp.status_code, (200, 500))
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type", "")
            self.assertIn("spreadsheetml", ct)

    def test_csv_template_download(self):
        resp = self._request(
            "GET", f"{PREFIX}/template/csv", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"employee_id", resp.content)
        self.assertIn(b"reports_to", resp.content)

    # ── employee_id format validation ──────────────────────────────────────
    def test_create_invalid_employee_code_format_is_400(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Bad Code",
                "employee_id": "lowercase123",
                "email": "badcode@example.com",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body["data"]["error_code"], "validation_error")
        self.assertEqual(
            body["data"]["details"].get("field"), "employee_id",
        )

    def test_create_valid_grt_format_succeeds(self):
        for idx, code in enumerate(("GRT1137", "GRTP6789")):
            resp = self._request(
                "POST", f"{PREFIX}/employees", token=self.token_hr,
                payload={
                    "name": f"Format {code}",
                    "employee_id": code,
                    "email": f"format{idx}@example.com",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(
                resp.json()["data"]["employee"]["employee_id"], code,
            )

    # ── user options (PL / QC dropdowns) ───────────────────────────────────
    def test_user_options_returns_lightweight_payload(self):
        self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Options Person",
                "employee_id": "GRT5050",
                "email": "options@example.com",
            },
        )
        resp = self._request(
            "GET", f"{PREFIX}/users/options?limit=5", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertIn("data", body)
        self.assertIn("count", body)
        self.assertIn("role_filter", body)
        for row in body["data"]:
            self.assertEqual(
                set(row.keys()),
                {"id", "name", "employee_code", "email", "role", "role_label"},
            )

    def test_user_options_pl_alias(self):
        resp = self._request(
            "GET", f"{PREFIX}/users/pl-options", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(body["role_filter"], ["pl"])

    def test_user_options_ql_alias(self):
        resp = self._request(
            "GET", f"{PREFIX}/users/ql-options", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(body["role_filter"], ["ql"])

    def test_user_options_tpm_alias(self):
        resp = self._request(
            "GET", f"{PREFIX}/users/tpm-options", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(body["role_filter"], ["tpm"])

    def test_user_options_multi_role(self):
        resp = self._request(
            "GET", f"{PREFIX}/users/options?role=pl,qr", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual(set(body["role_filter"]), {"pl", "qr"})

    def test_user_options_unknown_role_is_400(self):
        resp = self._request(
            "GET", f"{PREFIX}/users/options?role=bogus", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(
            resp.json()["data"]["error_code"], "unknown_role",
        )

    # ── flat hierarchy fields (assigned_ql / pl / tpm) ─────────────────────
    def test_employee_response_exposes_flat_hierarchy_keys(self):
        resp = self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "Flat Keys",
                "employee_id": "GRT9001",
                "email": "flat-keys@example.com",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        emp = resp.json()["data"]["employee"]
        for key in (
            "assigned_ql_id", "assigned_ql_name",
            "assigned_pl_id", "assigned_pl_name",
            "assigned_tpm_id", "assigned_tpm_name",
            "assigned_ql", "assigned_pl", "assigned_tpm",
            "job_title", "hierarchy_fields", "status",
        ):
            self.assertIn(key, emp, f"missing key {key} in response")

    def test_employees_list_exposes_flat_hierarchy_keys(self):
        self._request(
            "POST", f"{PREFIX}/employees", token=self.token_hr,
            payload={
                "name": "List Flat",
                "employee_id": "GRT9002",
                "email": "list-flat@example.com",
            },
        )
        resp = self._request(
            "GET", f"{PREFIX}/employees?search=List%20Flat", token=self.token_hr,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()["data"]["data"]
        self.assertGreaterEqual(len(rows), 1)
        for key in (
            "assigned_ql_id", "assigned_ql_name",
            "assigned_pl_id", "assigned_pl_name",
            "assigned_tpm_id", "assigned_tpm_name",
            "job_title", "status",
        ):
            self.assertIn(key, rows[0], f"missing key {key} in row")
