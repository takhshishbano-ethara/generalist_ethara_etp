from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "lynceus")
class TestStateMachine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tasker = cls.env["res.users"].create({
            "name": "Tasker One",
            "login": "tasker_one@test.lynceus",
            "email": "tasker_one@test.lynceus",
            "groups_id": [(4, cls.env.ref("lynceus.group_lynceus_tasker").id)],
        })

    def _make_prompt(self, content="phone photo of a desk"):
        return self.env["lynceus.prompt"].create({"content": content})

    def test_default_state_is_available(self):
        p = self._make_prompt()
        self.assertEqual(p.state, "available")
        self.assertFalse(p.assigned_user_id)

    def test_terminal_state_blocks_changes(self):
        p = self._make_prompt(content="terminal state guard")
        p.write({"state": "assigned", "assigned_user_id": self.tasker.id})
        p.action_submit()
        self.assertEqual(p.state, "used")
        with self.assertRaises(UserError):
            p.with_user(self.tasker).action_mark_bad("retry")

    def test_bad_requires_remarks(self):
        p = self._make_prompt(content="bad needs remarks")
        p.write({"state": "assigned", "assigned_user_id": self.tasker.id})
        with self.assertRaises(UserError):
            p.action_mark_bad("")

    def test_invalid_state_combination_rejected(self):
        p = self._make_prompt(content="state invariant")
        with self.assertRaises(ValidationError):
            p.write({"state": "assigned", "assigned_user_id": False})
