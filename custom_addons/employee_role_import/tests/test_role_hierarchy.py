"""Unit tests for role hierarchy + auto-assignment logic."""

from .common import EmployeeRoleImportCase
from ..services.employee_service import EmployeeService, EmployeeServiceError
from ..models.hr_employee import ROLE_DEFAULT_PARENT_ROLE, ROLE_GROUP_MAP


class TestRoleHierarchy(EmployeeRoleImportCase):

    def setUp(self):
        super().setUp()
        self.service = EmployeeService(self.env)

    def test_hierarchy_exposes_all_roles(self):
        result = self.service.role_hierarchy()
        keys = {r["key"] for r in result["roles"]}
        self.assertEqual(keys, set(ROLE_GROUP_MAP.keys()))

    def test_hierarchy_default_parent_map_matches_constant(self):
        result = self.service.role_hierarchy()
        self.assertEqual(result["default_parent_map"], dict(ROLE_DEFAULT_PARENT_ROLE))

    def test_hierarchy_chains_start_at_lowest_level(self):
        result = self.service.role_hierarchy()
        self.assertGreater(len(result["chains"]), 0)
        # The first chain should start with the lowest level
        first_chain = result["chains"][0]
        self.assertLessEqual(first_chain[0]["level"], first_chain[-1]["level"])

    def test_auto_assign_picks_senior_manager(self):
        # Build a small hierarchy: TPM -> PL -> QL (we assign manager to QL)
        tpm_user = self._make_user_for("Tpm", "tpm@example.com")
        tpm = self._make_employee(code="T1", name="Tpm",
                                 email="tpm@example.com", user=tpm_user)
        tpm.sudo().role = "tpm"

        pl_user = self._make_user_for("Pl", "pl@example.com")
        pl = self._make_employee(code="P1", name="Pl",
                                email="pl@example.com", user=pl_user)
        pl.sudo().role = "pl"

        ql_user = self._make_user_for("Ql", "ql@example.com")
        ql = self._make_employee(code="Q1", name="Ql",
                                email="ql@example.com", user=ql_user)
        ql.sudo().role = "ql"

        result = self.service.auto_assign_manager(ql.id)
        self.assertEqual(result["manager_id"], pl.id)
        self.assertEqual(result["manager_role"], "pl")

    def test_auto_assign_falls_back_to_parent_of_parent(self):
        # No PL exists - QL should fall through to TPM (PL -> TPM in chain).
        tpm_user = self._make_user_for("Tpm2", "tpm2@example.com")
        tpm = self._make_employee(code="T2", name="Tpm2",
                                 email="tpm2@example.com", user=tpm_user)
        tpm.sudo().role = "tpm"

        ql_user = self._make_user_for("Ql2", "ql2@example.com")
        ql = self._make_employee(code="Q2", name="Ql2",
                                email="ql2@example.com", user=ql_user)
        ql.sudo().role = "ql"

        result = self.service.auto_assign_manager(ql.id)
        self.assertEqual(result["manager_id"], tpm.id)

    def test_auto_assign_no_role_raises(self):
        emp = self._make_employee(code="NR1", name="NoRole",
                                 email="nr1@example.com")
        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.auto_assign_manager(emp.id)
        self.assertEqual(ctx.exception.code, "validation_error")

    def test_auto_assign_no_manager_available_raises_404(self):
        ql_user = self._make_user_for("Lonely", "lonely@example.com")
        ql = self._make_employee(code="L1", name="Lonely",
                                email="lonely@example.com", user=ql_user)
        ql.sudo().role = "ql"

        with self.assertRaises(EmployeeServiceError) as ctx:
            self.service.auto_assign_manager(ql.id)
        self.assertEqual(ctx.exception.http_status, 404)
