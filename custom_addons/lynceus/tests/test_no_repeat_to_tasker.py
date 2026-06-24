from odoo.tests import TransactionCase, tagged

from ..services import allocator


@tagged("post_install", "-at_install", "lynceus")
class TestNoRepeatToTasker(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tasker = cls.env["res.users"].create({
            "name": "Tasker R",
            "login": "tasker_r@test.lynceus",
            "email": "tasker_r@test.lynceus",
            "groups_id": [(4, cls.env.ref("lynceus.group_lynceus_tasker").id)],
        })

    def _seed_pool(self, n):
        Prompt = self.env["lynceus.prompt"]
        return Prompt.create([{"content": f"Pool prompt {i}"} for i in range(n)])

    def test_anti_join_excludes_history(self):
        prompts = self._seed_pool(5)
        first = allocator.allocate_to_user(self.env, self.tasker.id, 3)
        self.assertEqual(first, 3)
        for p in prompts[:3]:
            p.invalidate_recordset()
        prompts[0].action_submit()
        prompts[1].action_mark_bad("issue on multimango")
        prompts[2].write({"state": "available", "assigned_user_id": False, "outcome": False})

        second = allocator.allocate_to_user(self.env, self.tasker.id, 3)
        self.assertEqual(second, 2, "Reclaim-or-terminal prompts should NOT come back to same tasker (G1).")

    def test_no_double_allocation_to_same_tasker(self):
        self._seed_pool(2)
        first = allocator.allocate_to_user(self.env, self.tasker.id, 2)
        again = allocator.allocate_to_user(self.env, self.tasker.id, 2)
        self.assertEqual(first, 2)
        self.assertEqual(again, 0)
