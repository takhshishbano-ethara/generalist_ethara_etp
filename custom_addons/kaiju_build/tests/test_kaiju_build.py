# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

SAMPLE_DATASET_JSON = json.dumps(
    {
        "instance_id": "commit-0/test-repo",
        "repo": "Ethara-Ai/test-repo",
        "base_commit": "abc123",
        "reference_commit": "def456",
        "setup": {
            "install": "pip install -e .",
            "packages": "",
            "pip_packages": ["pytest"],
            "pre_install": [],
            "python": "3.10",
            "specification": "",
        },
        "test": {"test_cmd": "pytest", "test_dir": "tests"},
        "src_dir": "",
    }
)


@tagged("post_install", "-at_install")
class TestKaijuBuild(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.App = cls.env["kaiju.app"]
        cls.Build = cls.env["kaiju.build"]
        cls.app_record = cls.App.create(
            {
                "name": "build-test-app",
                "repo_url": "test-org/build-repo",
            }
        )

    def _create_build(self, **overrides):
        vals = {
            "app_id": self.app_record.id,
            "repo_name": "build-repo",
            "dataset_json": SAMPLE_DATASET_JSON,
        }
        vals.update(overrides)
        return self.Build.create(vals)

    def test_create_build(self):
        build = self._create_build()
        self.assertEqual(build.status, "draft")
        self.assertEqual(build.app_name, "build-test-app")
        self.assertEqual(build.repo_name, "build-repo")

    def test_dataset_json_stored(self):
        build = self._create_build()
        self.assertTrue(build.dataset_json)
        parsed = json.loads(build.dataset_json)
        self.assertEqual(parsed["instance_id"], "commit-0/test-repo")
        self.assertEqual(parsed["repo"], "Ethara-Ai/test-repo")

    def test_status_field_values(self):
        build = self._create_build()
        self.assertIn(
            build.status, ["draft", "queued", "building", "success", "failed", "error"]
        )

    def test_action_build_raises_without_k8s(self):
        build = self._create_build()
        with patch("odoo.addons.kaiju_build.models.kaiju_build.K8S_AVAILABLE", False):
            with self.assertRaises(UserError):
                build.action_build()

    def test_action_build_raises_without_dataset_json(self):
        build = self._create_build(dataset_json="")
        with patch("odoo.addons.kaiju_build.models.kaiju_build.K8S_AVAILABLE", True):
            with self.assertRaises(UserError):
                build.action_build()

    def test_repo_name_required(self):
        with self.assertRaises(Exception):
            self.Build.create(
                {
                    "app_id": self.app_record.id,
                    "dataset_json": SAMPLE_DATASET_JSON,
                }
            )
