# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_editor_s3")
class TestQcSeedResolver(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.settings_svc = self.env["video.editor.s3.settings"]
        self.ICP.set_param("video_editor_s3.qc_seed_file", "")
        self.ICP.set_param("video_editor_s3.qc_seed_filename", "")

    def test_falls_back_to_bundled_default_when_nothing_set(self):
        result = self.settings_svc.get_qc_seed_prompt()
        self.assertIn("Prompt QC Seed", result)

    def test_uploaded_file_returns_decoded_content(self):
        b64 = base64.b64encode("UPLOADED FILE PROMPT".encode("utf-8")).decode("ascii")
        self.ICP.set_param("video_editor_s3.qc_seed_file", b64)
        self.assertEqual(self.settings_svc.get_qc_seed_prompt(), "UPLOADED FILE PROMPT")

    def test_invalid_base64_falls_back_to_default(self):
        self.ICP.set_param("video_editor_s3.qc_seed_file", "!!!not-base64!!!")
        self.assertIn("Prompt QC Seed", self.settings_svc.get_qc_seed_prompt())

    def test_invalid_utf8_in_file_falls_back_to_default(self):
        b64 = base64.b64encode(b"\xff\xfe\xfd").decode("ascii")
        self.ICP.set_param("video_editor_s3.qc_seed_file", b64)
        self.assertIn("Prompt QC Seed", self.settings_svc.get_qc_seed_prompt())

    def test_empty_decoded_file_falls_back_to_default(self):
        b64 = base64.b64encode(b"   \n  ").decode("ascii")
        self.ICP.set_param("video_editor_s3.qc_seed_file", b64)
        self.assertIn("Prompt QC Seed", self.settings_svc.get_qc_seed_prompt())

    def test_setting_save_rejects_non_md_txt_extension(self):
        config = self.env["res.config.settings"].create({
            "video_editor_s3_qc_seed_file": base64.b64encode(b"content"),
            "video_editor_s3_qc_seed_filename": "evil.exe",
        })
        with self.assertRaises(ValidationError):
            config.set_values()

    def test_setting_save_rejects_oversized_file(self):
        big = b"x" * (101 * 1024)
        config = self.env["res.config.settings"].create({
            "video_editor_s3_qc_seed_file": base64.b64encode(big),
            "video_editor_s3_qc_seed_filename": "huge.md",
        })
        with self.assertRaises(ValidationError):
            config.set_values()

    def test_setting_save_rejects_invalid_utf8(self):
        config = self.env["res.config.settings"].create({
            "video_editor_s3_qc_seed_file": base64.b64encode(b"\xff\xfe\xfd"),
            "video_editor_s3_qc_seed_filename": "bad.md",
        })
        with self.assertRaises(ValidationError):
            config.set_values()

    def test_setting_save_persists_valid_file_to_icp(self):
        content = "Hello QC seed file!\n## Rubric\n- score 0-100\n"
        b64 = base64.b64encode(content.encode("utf-8"))
        config = self.env["res.config.settings"].create({
            "video_editor_s3_qc_seed_file": b64,
            "video_editor_s3_qc_seed_filename": "seed.md",
        })
        config.set_values()
        self.assertEqual(
            self.ICP.get_param("video_editor_s3.qc_seed_file"),
            b64.decode("ascii"),
        )
        self.assertEqual(
            self.ICP.get_param("video_editor_s3.qc_seed_filename"),
            "seed.md",
        )
        self.assertEqual(self.settings_svc.get_qc_seed_prompt(), content.strip())

    def test_setting_save_clears_file_when_unset(self):
        self.ICP.set_param(
            "video_editor_s3.qc_seed_file",
            base64.b64encode(b"old content").decode("ascii"),
        )
        config = self.env["res.config.settings"].create({
            "video_editor_s3_qc_seed_file": False,
        })
        config.set_values()
        self.assertFalse(self.ICP.get_param("video_editor_s3.qc_seed_file"))

    def test_settings_get_values_loads_file_from_icp(self):
        b64 = base64.b64encode(b"Stored content").decode("ascii")
        self.ICP.set_param("video_editor_s3.qc_seed_file", b64)
        config = self.env["res.config.settings"].create({})
        values = config.get_values()
        self.assertEqual(
            values["video_editor_s3_qc_seed_file"],
            b64.encode("ascii"),
        )
