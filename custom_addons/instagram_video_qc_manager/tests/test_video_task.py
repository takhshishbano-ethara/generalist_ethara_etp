# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_qc")
class TestVideoTask(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Task = self.env["video.task"]

    def test_creation_generates_sequence(self):
        t = self.Task.create({"description": "hello"})
        self.assertTrue(t.name and t.name != "New", "Sequence should fill the name.")

    def test_invalid_instagram_url_raises(self):
        with self.assertRaises(ValidationError):
            self.Task.create(
                {
                    "description": "x",
                    "original_video_1_url": "https://example.com/not-instagram",
                }
            )

    def test_valid_instagram_url_accepted(self):
        self.Task.create(
            {
                "description": "x",
                "original_video_1_url": "https://www.instagram.com/reel/AAA-bbb_111/",
            }
        )

    def test_create_new_version_increments(self):
        task = self.Task.create({"description": "x"})
        v1 = task.create_new_version()
        v2 = task.create_new_version()
        self.assertEqual(v1.version_no, 1)
        self.assertEqual(v2.version_no, 2)
        self.assertTrue(v2.is_latest)
        self.assertFalse(v1.is_latest)
        self.assertEqual(task.total_versions_count, 2)
        self.assertEqual(task.latest_version_id, v2)
