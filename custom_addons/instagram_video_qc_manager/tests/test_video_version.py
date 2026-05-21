# -*- coding: utf-8 -*-
import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_qc")
class TestVideoVersion(TransactionCase):
    def setUp(self):
        super().setUp()
        self.task = self.env["video.task"].create({"description": "t"})
        self.version = self.task.create_new_version()

    def test_write_editing_config_persists_dedicated_columns(self):
        config = {
            "trim": {"start": 1.0, "end": 5.5},
            "crop": {"x": 0, "y": 0, "w": 720, "h": 1280, "aspect": "9:16"},
            "brightness": 0.1,
        }
        self.version.write_editing_config(config)
        self.assertEqual(self.version.trim_start, 1.0)
        self.assertEqual(self.version.trim_end, 5.5)
        self.assertTrue(self.version.crop_data_json)
        decoded = json.loads(self.version.editing_json)
        self.assertEqual(decoded["trim"]["end"], 5.5)

    def test_record_qc_sets_user_and_date(self):
        self.version._record_qc("approved", "looks good")
        self.assertEqual(self.version.qc_status, "approved")
        self.assertEqual(self.version.qc_user, self.env.user)
        self.assertTrue(self.version.qc_date)
