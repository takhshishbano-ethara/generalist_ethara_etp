"""Tests for model-level Employee<->User mapping and mandatory hierarchy.

Verifies the rules hold regardless of entry point (plain ORM create/write,
which is what UI / API / import / scripts all funnel through).

NOTE: hr.employee.work_email must be an @ethara.ai address (pre-existing
constraint in task_forge_bridge), so all fixtures use @ethara.ai emails.
"""

from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .common import EmployeeRoleImportCase


@tagged("post_install", "-at_install", "employee_role_import")
class TestEmployeeUserMapping(EmployeeRoleImportCase):
    """Employee <-> User auto-linking via create()/write()."""

    def _plain_employee(self, code, email, **kw):
        # No role group -> role stays empty -> mandatory hierarchy does not apply.
        vals = {"name": code, "employee_code": code, "work_email": email}
        vals.update(kw)
        return self.env["hr.employee"].sudo().create(vals)

    def test_employee_create_links_existing_user(self):
        user = self._make_user_for("Map A", "mapa@ethara.ai")
        emp = self._plain_employee("MAPA", "mapa@ethara.ai")
        self.assertEqual(emp.user_id, user, "employee.create should link the matching user")

    def test_employee_create_links_case_insensitive(self):
        user = self._make_user_for("Map CI", "mapci@ethara.ai")
        emp = self._plain_employee("MAPCI", "MapCI@ethara.ai")
        self.assertEqual(emp.user_id, user)

    def test_user_create_links_existing_employee(self):
        emp = self._plain_employee("MAPB", "mapb@ethara.ai")
        self.assertFalse(emp.user_id)
        user = self._make_user_for("Map B", "mapb@ethara.ai")
        emp.invalidate_recordset()
        self.assertEqual(emp.user_id, user, "user.create should link the matching employee")

    def test_no_match_leaves_unlinked(self):
        emp = self._plain_employee("LONE", "nouser@ethara.ai")
        self.assertFalse(emp.user_id)

    def test_mapping_is_idempotent_and_single(self):
        user = self._make_user_for("Map C", "mapc@ethara.ai")
        emp = self._plain_employee("MAPC", "mapc@ethara.ai")
        self.assertEqual(emp.user_id, user)
        emp._etp_link_user()  # re-run: no change, no duplicate link
        self.assertEqual(emp.user_id, user)
        self.assertEqual(
            self.env["hr.employee"].sudo().search_count([("user_id", "=", user.id)]), 1
        )

    def test_does_not_steal_user_linked_elsewhere(self):
        # User's login is "shared@", but it's already linked to emp1 (whose own
        # work_email differs). emp2's work_email matches the login -> the linker
        # must NOT steal the already-linked user. (work_email is unique, so the
        # two employees deliberately use different work_emails.)
        user = self._make_user_for("Map D", "shared@ethara.ai")
        emp1 = self._plain_employee("MAPD1", "mapd1@ethara.ai", user_id=user.id)
        self.assertEqual(emp1.user_id, user)
        emp2 = self._plain_employee("MAPD2", "shared@ethara.ai")
        self.assertFalse(emp2.user_id, "must not steal a user already linked to emp1")


@tagged("post_install", "-at_install", "employee_role_import")
class TestMandatoryHierarchy(EmployeeRoleImportCase):
    """Tasker->QL/QR, QL/QR->PL, PL->TPM mandatory; TPM/CTO/HR none; QL==QR."""

    def _user_with_role(self, login, group_xmlid):
        return (
            self.env["res.users"]
            .sudo()
            .with_context(no_reset_password=True)
            .create({
                "name": login,
                "login": login,
                "email": login,
                "group_ids": [(6, 0, [
                    self.env.ref("base.group_user").id,
                    self.env.ref(group_xmlid).id,
                ])],
            })
        )

    def _emp(self, code, group_xmlid, parent=None):
        email = code.lower() + "@ethara.ai"
        user = self._user_with_role(email, group_xmlid)
        vals = {
            "name": code,
            "employee_code": code,
            "work_email": email,
            "user_id": user.id,
        }
        if parent:
            vals["parent_id"] = parent.id
        return self.env["hr.employee"].sudo().create(vals)

    def setUp(self):
        super().setUp()
        # Valid chain, built top-down: TPM -> PL -> QL -> tasker
        self.e_tpm = self._emp("HTPM", "etp_user_roles.group_tpm")
        self.e_pl = self._emp("HPL", "etp_user_roles.group_project_lead", parent=self.e_tpm)
        self.e_ql = self._emp("HQL", "etp_user_roles.group_quality_lead", parent=self.e_pl)

    def test_valid_chain_builds(self):
        self.assertEqual(self.e_pl.role, "pl")
        self.assertEqual(self.e_pl.task_forge_tpm_id, self.e_tpm)
        self.assertEqual(self.e_ql.role, "ql")
        self.assertEqual(self.e_ql.task_forge_pl_id, self.e_pl)
        # QL/QR auto-inherits its PL's TPM (only PL is manually chosen).
        self.assertEqual(self.e_ql.task_forge_tpm_id, self.e_tpm)

    def test_tpm_needs_no_manager(self):
        self.assertEqual(self.e_tpm.role, "tpm")  # created in setUp with no parent

    def test_tasker_requires_quality_manager(self):
        with self.assertRaises(ValidationError):
            self._emp("HTASKBAD", "etp_user_roles.group_tasker")  # no QL/QR

    def test_tasker_with_quality_manager_ok(self):
        tasker = self._emp("HTASKOK", "etp_user_roles.group_tasker", parent=self.e_ql)
        self.assertEqual(tasker.role, "tasker")
        self.assertEqual(tasker.task_forge_ql_id, self.e_ql)
        # Tasker manually picks only QL/QR; PL and TPM auto-fill from that QL.
        self.assertEqual(tasker.task_forge_pl_id, self.e_pl)
        self.assertEqual(tasker.task_forge_tpm_id, self.e_tpm)

    def test_ql_requires_pl(self):
        with self.assertRaises(ValidationError):
            self._emp("HQLBAD", "etp_user_roles.group_quality_lead")  # no PL

    def test_qr_treated_like_ql_requires_pl(self):
        with self.assertRaises(ValidationError):
            self._emp("HQRBAD", "etp_user_roles.group_quality_reviewer")  # no PL
        qr_ok = self._emp("HQROK", "etp_user_roles.group_quality_reviewer", parent=self.e_pl)
        self.assertEqual(qr_ok.role, "qr")
        self.assertEqual(qr_ok.task_forge_pl_id, self.e_pl)

    def test_pl_requires_tpm(self):
        with self.assertRaises(ValidationError):
            self._emp("HPLBAD", "etp_user_roles.group_project_lead")  # no TPM
