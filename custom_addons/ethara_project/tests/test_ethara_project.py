from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ethara_project.models.role_map import (
    ROLE_XML_IDS,
    VALID_ROLE_KEYS,
    resolve_role_ids,
)


@tagged('post_install', '-at_install', 'ethara_project')
class EtharaProjectTestBase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env['ethara.project']
        cls.Attachment = cls.env['ethara.project.attachment']
        cls.Employee = cls.env['hr.employee']
        cls.Users = cls.env['res.users']

        cls.tpm_role = cls.env.ref('api_auth_gateway.role_tpm_technical')
        cls.pl_role = cls.env.ref('api_auth_gateway.role_pl_technical')
        cls.qc_role = cls.env.ref('api_auth_gateway.role_qc_technical')
        cls.qr_role = cls.env.ref('api_auth_gateway.role_qc_non_stem')
        cls.rnd_role = cls.env.ref('api_auth_gateway.role_rnd_technical')

        cls.tpm_emp = cls._make_employee('TPM Alice', 'tpm.alice@example.com', cls.tpm_role)
        cls.pl_emp = cls._make_employee('PL Bob', 'pl.bob@example.com', cls.pl_role)
        cls.qc_emp = cls._make_employee('QC Carol', 'qc.carol@example.com', cls.qc_role)
        cls.qr_emp = cls._make_employee('QR Dave', 'qr.dave@example.com', cls.qr_role)
        cls.rnd_emp = cls._make_employee('RnD Eve', 'rnd.eve@example.com', cls.rnd_role)

        cls.plain_emp = cls._make_employee('Plain Frank', 'plain.frank@example.com', role=False)

    @classmethod
    def _make_employee(cls, name, login, role):
        vals = {'name': name, 'login': login, 'email': login}
        if role:
            vals['user_role'] = role.id
        user = cls.Users.create(vals)
        emp = cls.Employee.create({
            'name': name,
            'user_id': user.id,
            'work_email': login,
        })
        return emp


@tagged('post_install', '-at_install', 'ethara_project')
class TestRoleMap(EtharaProjectTestBase):

    def test_valid_role_keys(self):
        self.assertEqual(
            set(VALID_ROLE_KEYS),
            {'tpm', 'pl', 'qc', 'qr', 'rnd', 'pl_ql'},
        )

    def test_resolve_tpm_role(self):
        ids = resolve_role_ids(self.env, 'tpm')
        self.assertIn(self.tpm_role.id, ids)

    def test_resolve_rnd_role(self):
        ids = resolve_role_ids(self.env, 'rnd')
        self.assertIn(self.rnd_role.id, ids)

    def test_pl_ql_union(self):
        pl_ql_ids = set(resolve_role_ids(self.env, 'pl_ql'))
        pl_ids = set(resolve_role_ids(self.env, 'pl'))
        qc_ids = set(resolve_role_ids(self.env, 'qc'))
        qr_ids = set(resolve_role_ids(self.env, 'qr'))
        self.assertEqual(pl_ql_ids, pl_ids | qc_ids | qr_ids)

    def test_unknown_key_returns_empty(self):
        self.assertEqual(resolve_role_ids(self.env, 'no-such-key'), [])

    def test_all_xml_ids_resolve(self):
        for key, xml_ids in ROLE_XML_IDS.items():
            resolved = resolve_role_ids(self.env, key)
            self.assertEqual(
                len(resolved), len(xml_ids),
                f"Role key '{key}': not all XML IDs resolved. "
                f"Expected {len(xml_ids)}, got {len(resolved)}",
            )


@tagged('post_install', '-at_install', 'ethara_project')
class TestEtharaProjectModel(EtharaProjectTestBase):

    def test_minimal_create(self):
        p = self.Project.create({
            'name': 'Project X',
            'client_name': 'ClientCo',
        })
        self.assertTrue(p.id)
        self.assertEqual(p.state, 'start')
        self.assertEqual(p.attachment_count, 0)

    def test_date_constraint(self):
        with self.assertRaises(ValidationError):
            self.Project.create({
                'name': 'Bad Dates',
                'client_name': 'ClientCo',
                'start_date': date(2026, 12, 1),
                'end_date': date(2026, 6, 1),
            })

    def test_missing_name_required(self):
        with self.assertRaises(Exception):
            self.Project.create({'client_name': 'ClientCo'})

    def test_attachment_url_only(self):
        p = self.Project.create({
            'name': 'With Docs',
            'client_name': 'ClientCo',
            'attachment_ids': [(0, 0, {
                'name': 'Brief',
                'attachment_url': 'https://example.com/brief.pdf',
            })],
        })
        self.assertEqual(p.attachment_count, 1)
        self.assertEqual(p.attachment_ids[0].attachment_url,
                         'https://example.com/brief.pdf')

    def test_attachment_url_required(self):
        p = self.Project.create({'name': 'X', 'client_name': 'Y'})
        with self.assertRaises(ValidationError):
            self.Attachment.create({
                'project_id': p.id,
                'name': 'Empty',
                'attachment_url': '',
            })

    def test_team_assignment_stores_ids(self):
        p = self.Project.create({
            'name': 'Team Test',
            'client_name': 'ClientCo',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
            'assigned_pl_ql_ids': [(6, 0, [self.pl_emp.id, self.qc_emp.id])],
            'assigned_rnd_ids': [(6, 0, [self.rnd_emp.id])],
        })
        self.assertIn(self.tpm_emp, p.assigned_tpm_ids)
        self.assertIn(self.pl_emp, p.assigned_pl_ql_ids)
        self.assertIn(self.qc_emp, p.assigned_pl_ql_ids)
        self.assertIn(self.rnd_emp, p.assigned_rnd_ids)


@tagged('post_install', '-at_install', 'ethara_project')
class TestStateTransitions(EtharaProjectTestBase):

    def test_default_state_start(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        self.assertEqual(p.state, 'start')

    def test_action_pause(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        p.action_pause()
        self.assertEqual(p.state, 'pause')

    def test_action_close(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        p.action_close()
        self.assertEqual(p.state, 'close')

    def test_action_complete(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        p.action_complete()
        self.assertEqual(p.state, 'complete')

    def test_action_start_after_pause(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        p.action_pause()
        p.action_start()
        self.assertEqual(p.state, 'start')

    def test_action_set_state_rejects_invalid(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        with self.assertRaises(ValidationError):
            p.action_set_state('not-a-state')


@tagged('post_install', '-at_install', 'ethara_project')
class TestWorkStatusCascade(EtharaProjectTestBase):

    def test_default_unallocated(self):
        self.assertEqual(self.tpm_emp.work_status, 'unallocated')

    def test_allocated_on_create_with_state_start(self):
        self.Project.create({
            'name': 'P1',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        self.assertEqual(self.tpm_emp.work_status, 'allocated')

    def test_unallocated_when_paused(self):
        p = self.Project.create({
            'name': 'P1',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        self.assertEqual(self.tpm_emp.work_status, 'allocated')
        p.action_pause()
        self.assertEqual(self.tpm_emp.work_status, 'unallocated')

    def test_unallocated_when_closed(self):
        p = self.Project.create({
            'name': 'P1',
            'client_name': 'C',
            'assigned_pl_ql_ids': [(6, 0, [self.pl_emp.id])],
        })
        p.action_close()
        self.assertEqual(self.pl_emp.work_status, 'unallocated')

    def test_unallocated_when_completed(self):
        p = self.Project.create({
            'name': 'P1',
            'client_name': 'C',
            'assigned_rnd_ids': [(6, 0, [self.rnd_emp.id])],
        })
        p.action_complete()
        self.assertEqual(self.rnd_emp.work_status, 'unallocated')

    def test_stays_allocated_if_on_another_started_project(self):
        p1 = self.Project.create({
            'name': 'P1',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        self.Project.create({
            'name': 'P2',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        self.assertEqual(self.tpm_emp.work_status, 'allocated')
        p1.action_pause()
        self.assertEqual(
            self.tpm_emp.work_status, 'allocated',
            'Employee should stay allocated because P2 is still in start.',
        )

    def test_reallocated_when_project_restarted(self):
        p = self.Project.create({
            'name': 'P',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        p.action_pause()
        self.assertEqual(self.tpm_emp.work_status, 'unallocated')
        p.action_start()
        self.assertEqual(self.tpm_emp.work_status, 'allocated')

    def test_removed_from_team_flips_to_unallocated(self):
        p = self.Project.create({
            'name': 'P',
            'client_name': 'C',
            'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])],
        })
        self.assertEqual(self.tpm_emp.work_status, 'allocated')
        p.write({'assigned_tpm_ids': [(5, 0, 0)]})
        self.assertEqual(self.tpm_emp.work_status, 'unallocated')

    def test_added_to_team_flips_to_allocated(self):
        p = self.Project.create({'name': 'P', 'client_name': 'C'})
        self.assertEqual(self.tpm_emp.work_status, 'unallocated')
        p.write({'assigned_tpm_ids': [(6, 0, [self.tpm_emp.id])]})
        self.assertEqual(self.tpm_emp.work_status, 'allocated')


@tagged('post_install', '-at_install', 'ethara_project')
class TestEmployeeRoleDomain(EtharaProjectTestBase):

    def test_tpm_domain_allows_tpm_only(self):
        domain = [('user_id.user_role', 'in', resolve_role_ids(self.env, 'tpm'))]
        matches = self.Employee.search(domain)
        self.assertIn(self.tpm_emp, matches)
        self.assertNotIn(self.pl_emp, matches)
        self.assertNotIn(self.rnd_emp, matches)
        self.assertNotIn(self.plain_emp, matches)

    def test_pl_ql_domain_matches_pl_qc_qr(self):
        domain = [('user_id.user_role', 'in', resolve_role_ids(self.env, 'pl_ql'))]
        matches = self.Employee.search(domain)
        self.assertIn(self.pl_emp, matches)
        self.assertIn(self.qc_emp, matches)
        self.assertIn(self.qr_emp, matches)
        self.assertNotIn(self.tpm_emp, matches)
        self.assertNotIn(self.rnd_emp, matches)

    def test_rnd_domain_matches_rnd_only(self):
        domain = [('user_id.user_role', 'in', resolve_role_ids(self.env, 'rnd'))]
        matches = self.Employee.search(domain)
        self.assertIn(self.rnd_emp, matches)
        self.assertNotIn(self.tpm_emp, matches)
        self.assertNotIn(self.pl_emp, matches)
