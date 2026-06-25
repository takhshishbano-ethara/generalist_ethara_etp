from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..services import allocator, reclaimer


@tagged("post_install", "-at_install", "lynceus")
class TestReclaim12h(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tasker = cls.env["res.users"].create({
            "name": "Idle Tasker",
            "login": "idle@test.lynceus",
            "email": "idle@test.lynceus",
            "groups_id": [(4, cls.env.ref("lynceus.group_lynceus_tasker").id)],
        })

    def _seed_pool(self, n):
        return self.env["lynceus.prompt"].create(
            [{"content": f"Reclaim test {i}"} for i in range(n)]
        )

    def test_idle_tasker_untouched_returns_to_pool(self):
        self._seed_pool(4)
        allocator.allocate_to_user(self.env, self.tasker.id, 4)
        self.tasker.lynceus_last_activity_at = fields.Datetime.now() - timedelta(hours=13)

        recovered = reclaimer.sweep(self.env, reclaim_hours=12)
        self.assertEqual(recovered, 4)
        available = self.env["lynceus.prompt"].search_count([("state", "=", "available")])
        self.assertEqual(available, 4)

    def test_active_tasker_keeps_queue(self):
        self._seed_pool(2)
        allocator.allocate_to_user(self.env, self.tasker.id, 2)
        self.tasker.lynceus_last_activity_at = fields.Datetime.now() - timedelta(hours=1)

        recovered = reclaimer.sweep(self.env, reclaim_hours=12)
        self.assertEqual(recovered, 0)

    def test_terminal_states_never_reclaimed(self):
        prompts = self._seed_pool(2)
        allocator.allocate_to_user(self.env, self.tasker.id, 2)
        prompts[0].action_submit()
        prompts[1].action_mark_bad("bad output")
        self.tasker.lynceus_last_activity_at = fields.Datetime.now() - timedelta(hours=48)

        recovered = reclaimer.sweep(self.env, reclaim_hours=12)
        self.assertEqual(recovered, 0, "USED and BAD must never reclaim (G10/G11).")
