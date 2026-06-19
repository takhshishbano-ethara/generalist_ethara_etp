"""Unit tests for EmployeeService (CRUD + validation + serialization)."""

from .common import EmployeeRoleImportCase
from ..services.employee_service import EmployeeService, EmployeeServiceError


class TestEmployeeCRUD(EmployeeRoleImportCase):

    def setUp(self):
        super().setUp()
        self.service = EmployeeService(self.env)

    def test_create_minimal_payload(self):
        result = self.service.create({
            "name": "Charlie",
            "employee_id": "C100",
            "email": "charlie@example.com",
        })
        self.assertEqual(result["employee_id"], "C100")
        self.assertEqual(result["email"], "charlie@example.com")
        self.assertTrue(result["active"])

    def test_create_missing_required_fields_raises(self):
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.create({"name": "No Code"})
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_create_duplicate_employee_id_raises_409(self):
        self._make_employee(code="D100", name="X", email="x@example.com")
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.create({
                "name": "Y",
                "employee_id": "D100",
                "email": "y@example.com",
            })
        self.assertEqual(ctx.exception.code, "duplicate_employee")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_create_duplicate_email_raises_409(self):
        self._make_employee(code="D101", name="X", email="dupe@example.com")
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.create({
                "name": "Y",
                "employee_id": "D102",
                "email": "dupe@example.com",
            })
        self.assertEqual(ctx.exception.code, "duplicate_employee")

    def test_create_invalid_email_raises(self):
        with self.assertRaises(EmployeeServiceError):
            self.service.create({
                "name": "Bad Email",
                "employee_id": "GRT100",
                "email": "not-an-email",
            })

    def test_create_invalid_employee_code_format_raises(self):
        for bad_code in ("grt1137", "GRT", "1137GRT", "GRT-1137", "G1", "GRTABC"):
            with self.assertRaises(EmployeeServiceError) as ctx:
                self.service.create({
                    "name": "Bad Code",
                    "employee_id": bad_code,
                    "email": f"bad-{bad_code.lower()}@example.com",
                })
            self.assertEqual(ctx.exception.code, "validation_error")
            self.assertEqual(ctx.exception.details.get("field"), "employee_id")

    def test_create_valid_employee_code_formats_succeed(self):
        for good_code in ("GRT1137", "GRTP6789", "EMP001", "ABCDEF999"):
            result = self.service.create({
                "name": f"Good {good_code}",
                "employee_id": good_code,
                "email": f"{good_code.lower()}@example.com",
            })
            self.assertEqual(result["employee_id"], good_code)

    def test_update_invalid_employee_code_format_raises(self):
        emp = self._make_employee(
            code="GRT2200", name="Updatable", email="upd@example.com",
        )
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.update(emp.id, {"employee_id": "lowercase123"})
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_get_by_id_returns_dict(self):
        emp = self._make_employee(code="G1", name="Get Me",
                                 email="getme@example.com")
        result = self.service.get(emp.id)
        self.assertEqual(result["id"], emp.id)
        self.assertEqual(result["name"], "Get Me")

    def test_get_missing_raises_404(self):
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.get(99999999)
        self.assertEqual(ctx.exception.http_status, 404)

    def test_list_pagination(self):
        for i in range(5):
            self._make_employee(
                code=f"L{i}", name=f"L{i}", email=f"l{i}@example.com",
            )
        page1 = self.service.list(page=1, limit=2, search=None)
        self.assertEqual(len(page1["data"]), 2)
        self.assertEqual(page1["pagination"]["page"], 1)
        self.assertGreaterEqual(page1["pagination"]["total_records"], 5)

    def test_list_search_filter(self):
        self._make_employee(code="S1", name="UniqueName",
                           email="s1@example.com")
        self._make_employee(code="S2", name="Other",
                           email="s2@example.com")
        result = self.service.list(search="UniqueName")
        names = [r["name"] for r in result["data"]]
        self.assertIn("UniqueName", names)
        self.assertNotIn("Other", names)

    def test_list_invalid_role_raises(self):
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.list(role="bogus")
        self.assertEqual(ctx.exception.code, "unknown_role")

    def test_update_name_and_email(self):
        emp = self._make_employee(code="U1", name="Old Name",
                                 email="old@example.com")
        result = self.service.update(emp.id, {
            "name": "New Name",
            "email": "new@example.com",
        })
        self.assertEqual(result["name"], "New Name")
        self.assertEqual(result["email"], "new@example.com")

    def test_update_to_duplicate_email_raises(self):
        self._make_employee(code="U2", name="Other",
                           email="other@example.com")
        target = self._make_employee(code="U3", name="Target",
                                    email="target@example.com")
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.update(target.id, {"email": "other@example.com"})
        self.assertEqual(ctx.exception.code, "duplicate_employee")

    def test_delete_hard_removes_record(self):
        emp = self._make_employee(code="D1", name="Doomed",
                                 email="d1@example.com")
        emp_id = emp.id
        result = self.service.delete(emp_id)
        self.assertTrue(result["deleted"])
        gone = self.env["hr.employee"].sudo().with_context(active_test=False).browse(emp_id)
        self.assertFalse(gone.exists())

    def test_delete_detaches_direct_reports(self):
        parent = self._make_employee(code="P1", name="Parent",
                                    email="parent@example.com")
        child = self._make_employee(code="C1", name="Child",
                                   email="child@example.com",
                                   parent=parent)
        self.assertEqual(child.parent_id, parent)
        self.service.delete(parent.id)
        child_after = self.env["hr.employee"].sudo().browse(child.id)
        self.assertTrue(child_after.exists())
        self.assertFalse(child_after.parent_id)

    def test_serialize_exposes_flat_hierarchy_keys(self):
        emp = self._make_employee(
            code="GRT4001", name="Solo", email="solo@example.com",
        )
        payload = self.service.serialize(emp)
        for key in (
            "assigned_ql", "assigned_pl", "assigned_tpm",
            "assigned_ql_id", "assigned_ql_name",
            "assigned_pl_id", "assigned_pl_name",
            "assigned_tpm_id", "assigned_tpm_name",
            "job_title", "hierarchy_fields", "status",
        ):
            self.assertIn(key, payload)
        self.assertIsNone(payload["assigned_ql_id"])
        self.assertIsNone(payload["assigned_pl_id"])
        self.assertIsNone(payload["assigned_tpm_id"])


class TestEmployeeOptions(EmployeeRoleImportCase):

    def setUp(self):
        super().setUp()
        self.service = EmployeeService(self.env)

    def _seed_role(self, code, name, email, role_key):
        emp = self._make_employee(code=code, name=name, email=email)
        user = self._make_user_for(emp, login=email)
        emp.user_id = user
        emp.sudo().role = role_key
        return emp

    def test_list_options_returns_lightweight_shape(self):
        result = self.service.list_options(roles=["pl"], limit=10)
        self.assertIn("data", result)
        self.assertIn("count", result)
        self.assertIn("role_filter", result)
        self.assertEqual(result["role_filter"], ["pl"])
        for row in result["data"]:
            self.assertEqual(
                set(row.keys()),
                {"id", "name", "employee_code", "email", "role", "role_label"},
            )

    def test_list_options_filters_by_single_role(self):
        try:
            self._seed_role("GRT2001", "Pl One", "pl1@example.com", "pl")
            self._seed_role("GRT2002", "Qr One", "qr1@example.com", "qr")
        except Exception:
            self.skipTest("etp_user_roles groups not installed in this DB")
        pl = self.service.list_options(roles=["pl"], limit=50)
        emails = [r["email"] for r in pl["data"]]
        self.assertIn("pl1@example.com", emails)
        self.assertNotIn("qr1@example.com", emails)

    def test_list_options_supports_multi_role(self):
        try:
            self._seed_role("GRT2010", "Pl Multi", "plm@example.com", "pl")
            self._seed_role("GRT2011", "Qr Multi", "qrm@example.com", "qr")
        except Exception:
            self.skipTest("etp_user_roles groups not installed in this DB")
        both = self.service.list_options(roles=["pl", "qr"], limit=50)
        emails = [r["email"] for r in both["data"]]
        self.assertIn("plm@example.com", emails)
        self.assertIn("qrm@example.com", emails)
        self.assertEqual(set(both["role_filter"]), {"pl", "qr"})

    def test_list_options_unknown_role_raises(self):
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.list_options(roles=["bogus"])
        self.assertEqual(ctx.exception.code, "unknown_role")

    def test_list_options_search_filter(self):
        self._make_employee(
            code="GRT3001", name="Searchable Person", email="searchme@example.com",
        )
        result = self.service.list_options(search="Searchable", limit=10)
        names = [r["name"] for r in result["data"]]
        self.assertIn("Searchable Person", names)

    def test_list_options_excludes_given_employee(self):
        emp = self._make_employee(
            code="GRT3002", name="Self Pick", email="selfpick@example.com",
        )
        result = self.service.list_options(
            search="Self Pick", exclude_employee_id=emp.id, limit=10,
        )
        ids = [r["id"] for r in result["data"]]
        self.assertNotIn(emp.id, ids)
