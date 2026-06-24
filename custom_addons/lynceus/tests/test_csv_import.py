import base64

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "lynceus")
class TestCsvImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.users = cls.env["res.users"].create([
            {
                "name": "Alpha",
                "login": "alpha@test.lynceus",
                "email": "alpha@test.lynceus",
                "groups_id": [(4, cls.env.ref("lynceus.group_lynceus_tasker").id)],
            },
            {
                "name": "Beta",
                "login": "beta@test.lynceus",
                "email": "beta@test.lynceus",
                "groups_id": [(4, cls.env.ref("lynceus.group_lynceus_tasker").id)],
            },
        ])
        cls.env["lynceus.prompt"].create(
            [{"content": f"CSV-import seed {i}"} for i in range(10)]
        )

    def test_csv_import_and_allocate(self):
        csv = b"email,name\nalpha@test.lynceus,Alpha Updated\nbeta@test.lynceus,\n"
        wizard = self.env["lynceus.import.active.taskers.wizard"].create({
            "file": base64.b64encode(csv).decode(),
            "filename": "active.csv",
            "quota_override": 3,
        })
        wizard.action_import()
        self.assertEqual(wizard.imported_count, 2)
        self.assertEqual(wizard.allocated_count, 6)

        for user in self.users:
            count = self.env["lynceus.prompt"].search_count([
                ("assigned_user_id", "=", user.id),
                ("state", "=", "assigned"),
            ])
            self.assertEqual(count, 3, f"Tasker {user.name} should have 3 prompts")

    def test_csv_import_skips_unknown_email(self):
        csv = b"email,name\nghost@test.lynceus,Ghost\n"
        wizard = self.env["lynceus.import.active.taskers.wizard"].create({
            "file": base64.b64encode(csv).decode(),
            "filename": "ghost.csv",
        })
        wizard.action_import()
        self.assertEqual(wizard.imported_count, 0)
        self.assertIn("not found", wizard.error_log or "")
