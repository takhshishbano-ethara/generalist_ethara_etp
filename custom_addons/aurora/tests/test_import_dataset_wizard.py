import base64
import json
import os
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_S3_URL = "https://test-bucket.s3.us-east-1.amazonaws.com/aurora/aurora_phase2/testorg__testrepo/run_1/dataset.jsonl"
_S3_CONFIG = {
    "bucket": "test-bucket",
    "region": "us-east-1",
    "access_key": "key",
    "secret_key": "secret",
    "folder": "aurora",
}


@tagged("post_install", "-at_install")
class TestImportDatasetWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "aurora.output_dir", "/tmp/aurora_test"
        )
        cls.pipeline = cls.env["aurora.pipeline"].create({
            "github_org": "testorg",
            "github_repo": "testrepo",
        })
        cls.evaluation = cls.env["aurora.evaluation"].create({
            "pipeline_id": cls.pipeline.id,
        })

    def _make_jsonl_bytes(self, entries=None):
        if entries is None:
            entries = [
                {"org": "testorg", "repo": "testrepo", "number": 1},
                {"org": "testorg", "repo": "testrepo", "number": 2},
            ]
        content = "\n".join(json.dumps(e) for e in entries)
        return base64.b64encode(content.encode("utf-8")).decode()

    def _make_wizard(self, jsonl_b64=None, filename="dataset.jsonl", evaluation=None):
        return self.env["aurora.import.dataset.wizard"].create({
            "evaluation_id": (evaluation or self.evaluation).id,
            "jsonl_file": jsonl_b64 or self._make_jsonl_bytes(),
            "jsonl_filename": filename,
        })

    def test_rejects_non_jsonl_extension(self):
        wizard = self._make_wizard(filename="data.csv")
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_rejects_txt_extension(self):
        wizard = self._make_wizard(filename="data.txt")
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_rejects_empty_file(self):
        empty_b64 = base64.b64encode(b"").decode()
        wizard = self._make_wizard(jsonl_b64=empty_b64)
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_rejects_whitespace_only_file(self):
        blank_b64 = base64.b64encode(b"   \n  \n").decode()
        wizard = self._make_wizard(jsonl_b64=blank_b64)
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_rejects_invalid_json_content(self):
        bad_b64 = base64.b64encode(b"not json\n").decode()
        wizard = self._make_wizard(jsonl_b64=bad_b64)
        with self.assertRaises(UserError):
            wizard.action_import()

    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=False)
    def test_sets_dataset_file_locally(self, _mock):
        wizard = self._make_wizard()
        wizard.action_import()
        self.assertTrue(self.evaluation.dataset_file)
        self.assertTrue(os.path.isfile(self.evaluation.dataset_file))

    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=False)
    def test_no_s3_url_when_not_configured(self, _mock):
        wizard = self._make_wizard()
        wizard.action_import()
        self.assertFalse(self.evaluation.dataset_jsonl_url)

    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=False)
    def test_local_file_contains_correct_content(self, _mock):
        entries = [{"org": "testorg", "repo": "testrepo", "number": 42}]
        wizard = self._make_wizard(jsonl_b64=self._make_jsonl_bytes(entries))
        wizard.action_import()
        with open(self.evaluation.dataset_file, "r") as fh:
            loaded = json.loads(fh.readline())
        self.assertEqual(loaded["number"], 42)

    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value=_S3_CONFIG)
    @patch("odoo.addons.aurora.models.s3_storage.upload_file", return_value=_S3_URL)
    @patch("odoo.addons.aurora.models.s3_storage.get_next_run_number", return_value=1)
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_uploads_to_s3_when_configured(self, _ic, _rn, mock_upload, _lsc):
        wizard = self._make_wizard()
        wizard.action_import()
        mock_upload.assert_called_once()
        self.assertEqual(self.evaluation.dataset_jsonl_url, _S3_URL)

    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value=_S3_CONFIG)
    @patch("odoo.addons.aurora.models.s3_storage.upload_file", return_value=_S3_URL)
    @patch("odoo.addons.aurora.models.s3_storage.get_next_run_number", return_value=3)
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_sets_s3_run_number(self, _ic, _rn, _upload, _lsc):
        wizard = self._make_wizard()
        wizard.action_import()
        self.assertEqual(self.evaluation.s3_run_number, 3)

    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value=_S3_CONFIG)
    @patch("odoo.addons.aurora.models.s3_storage.upload_file", return_value=_S3_URL)
    @patch("odoo.addons.aurora.models.s3_storage.get_next_run_number", return_value=1)
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_s3_key_uses_phase2_pattern(self, _ic, _rn, mock_upload, _lsc):
        wizard = self._make_wizard()
        wizard.action_import()
        s3_key = mock_upload.call_args[0][2]
        self.assertIn("aurora_phase2", s3_key)
        self.assertIn("testorg__testrepo", s3_key)
        self.assertIn("run_1", s3_key)
        self.assertIn("dataset.jsonl", s3_key)

    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value=_S3_CONFIG)
    @patch("odoo.addons.aurora.models.s3_storage.upload_file", return_value=_S3_URL)
    @patch("odoo.addons.aurora.models.s3_storage.get_next_run_number", return_value=1)
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_s3_upload_local_path_points_to_existing_file(self, _ic, _rn, mock_upload, _lsc):
        wizard = self._make_wizard()
        wizard.action_import()
        local_path = mock_upload.call_args[0][1]
        self.assertTrue(os.path.isfile(local_path))

    @patch("odoo.addons.aurora.models.artifact_collector.load_s3_config", return_value=_S3_CONFIG)
    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=True)
    def test_skips_s3_upload_when_no_pipeline(self, _ic, _lsc):
        eval_no_pl = self.env["aurora.evaluation"].create({})
        wizard = self.env["aurora.import.dataset.wizard"].create({
            "evaluation_id": eval_no_pl.id,
            "jsonl_file": self._make_jsonl_bytes(),
            "jsonl_filename": "dataset.jsonl",
        })
        wizard.action_import()
        self.assertTrue(eval_no_pl.dataset_file)
        self.assertFalse(eval_no_pl.dataset_jsonl_url)
        self.assertFalse(eval_no_pl.s3_run_number)

    @patch("odoo.addons.aurora.models.s3_storage.is_configured", return_value=False)
    def test_returns_evaluation_form_action(self, _mock):
        wizard = self._make_wizard()
        result = wizard.action_import()
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_model"], "aurora.evaluation")
        self.assertEqual(result["res_id"], self.evaluation.id)
        self.assertEqual(result["view_mode"], "form")
        self.assertEqual(result["target"], "current")
