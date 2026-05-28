# -*- coding: utf-8 -*-
import os

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "video_editor_s3")
class TestMediaStorage(TransactionCase):

    def setUp(self):
        super().setUp()
        self.storage = self.env["video.editor.s3.media.storage"]
        self.project = self.env["video.editor.project"].create(
            {
                "name": "Test Storage Project",
                "s3_source_url": "s3://example-bucket/clips/sample.mp4",
            }
        )

    def test_media_root_is_writable(self):
        root = self.storage.get_media_root()
        self.assertTrue(os.path.isdir(root))
        self.assertTrue(os.access(root, os.W_OK))

    def test_project_dir_created(self):
        project_dir = self.storage.project_dir(self.project)
        self.assertTrue(os.path.isdir(project_dir))
        self.assertTrue(project_dir.endswith(str(self.project.id)))

    def test_path_for_source(self):
        path = self.storage.path_for(self.project, "source", version=1)
        self.assertTrue(path.endswith("v1_source.mp4"))

    def test_path_for_edited_with_slot(self):
        path = self.storage.path_for(self.project, "edited", version=2, slot=3)
        self.assertTrue(path.endswith("v2_edited_slot3.mp4"))

    def test_path_for_invalid_kind(self):
        with self.assertRaises(UserError):
            self.storage.path_for(self.project, "thumbnail")

    def test_relative_then_absolute_roundtrip(self):
        abs_path = self.storage.path_for(self.project, "preview", version=1)
        rel = self.storage.relative(abs_path)
        self.assertFalse(rel.startswith(os.sep))
        roundtrip = self.storage.absolute(rel)
        self.assertEqual(os.path.realpath(roundtrip), os.path.realpath(abs_path))

    def test_absolute_traversal_guard(self):
        with self.assertRaises(UserError):
            self.storage.absolute("../../etc/passwd")
