# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_qc")
class TestQCWorkflow(TransactionCase):
    def setUp(self):
        super().setUp()
        self.task = self.env["video.task"].create({"description": "QC cycle"})
        # Simulate downloaded originals via dummy attachments
        for slot in (1, 2):
            att = self.env["ir.attachment"].create(
                {
                    "name": f"src_{slot}.mp4",
                    "datas": b"AAAA",
                    "res_model": "video.task",
                    "res_id": self.task.id,
                    "mimetype": "video/mp4",
                }
            )
            self.task.write({f"original_video_{slot}_attachment": att.id})

    def test_full_cycle(self):
        self.task.action_mark_downloaded()
        self.assertEqual(self.task.state, "downloaded")

        self.task.action_start_editing()
        self.assertEqual(self.task.state, "editing")

        v1 = self.task.create_new_version()
        self.task.action_send_to_qc()
        self.assertEqual(self.task.state, "qc_pending")
        self.assertEqual(v1.qc_status, "pending")

        # Reject -> rework -> new version
        v1.action_qc_rework("Reshoot the hook")
        self.assertEqual(self.task.state, "editing")
        v2 = self.task.create_new_version()
        self.assertEqual(v2.version_no, 2)

        # Approve v2
        self.task.action_send_to_qc()
        v2.action_qc_approve("Great!")
        self.assertEqual(self.task.state, "qc_approved")

        self.task.action_complete()
        self.assertEqual(self.task.state, "completed")
        self.assertEqual(self.task.qc_approved_count, 1)
        self.assertEqual(self.task.qc_count, 2)  # rework + approved
