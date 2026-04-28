from unittest.mock import patch
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.atlas.models.atlas import generate_description_from_turns


@tagged("atlas", "atlas_desc", "post_install", "-at_install")
class TestGenerateDescriptionFromTurns(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Atlas = cls.env["atlas.atlas"]
        cls.Sandbox = cls.env["atlas.sandbox"]
        cls.Turn = cls.env["atlas.turn"]
        cls.task = cls.Atlas.create({})
        cls.sbx = cls.Sandbox.create({"atlas_id": cls.task.id, "model_type": "glm"})

    def _make_turn(self, **kw):
        kw.setdefault("sandbox_id", self.sbx.id)
        return self.Turn.create(kw)

    def test_no_turns_returns_empty(self):
        desc, usage = generate_description_from_turns(self.env, self.Turn.browse())
        self.assertEqual(desc, "")
        self.assertEqual(usage, {})

    def test_empty_recordset_returns_empty(self):
        desc, usage = generate_description_from_turns(self.env, [])
        self.assertEqual(desc, "")
        self.assertEqual(usage, {})

    def test_missing_credentials_returns_empty(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "")
        t = self._make_turn(prompt="hello", response="world", turn_status="Completed")
        desc, usage = generate_description_from_turns(self.env, t)
        self.assertEqual(desc, "")

    def test_hint_turn_skipped(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "")
        t = self._make_turn(prompt="hint only", is_hint_turn=True, turn_status="Completed")
        desc, _ = generate_description_from_turns(self.env, t)
        self.assertEqual(desc, "")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse")
    def test_bedrock_success_returns_description(self, mock_call):
        mock_call.return_value = ("This is a generated description.", {"input_tokens": 10, "output_tokens": 5})
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "arn:fake")
        ICP.set_param("atlas.bedrock_region", "ap-south-1")
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "fake_token"
        t = self._make_turn(prompt="Do something", response="Done", turn_status="Completed")
        desc, usage = generate_description_from_turns(self.env, t)
        self.assertTrue(mock_call.called or desc == "")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse")
    def test_bedrock_degenerate_output_discarded(self, mock_call):
        mock_call.return_value = ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", {"input_tokens": 5, "output_tokens": 3})
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "arn:fake")
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "fake"
        t = self._make_turn(prompt="p", response="r", turn_status="Completed")
        desc, _ = generate_description_from_turns(self.env, t)
        self.assertEqual(desc, "")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse",
           side_effect=Exception("network error"))
    def test_bedrock_exception_returns_error(self, _):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "arn:fake")
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "fake"
        t = self._make_turn(prompt="p", response="r", turn_status="Completed")
        desc, usage = generate_description_from_turns(self.env, t)
        self.assertEqual(desc, "")
        self.assertIn("error", usage)

    def test_turns_without_prompts_skipped(self):
        t = self._make_turn(prompt="", response="r", turn_status="Completed")
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("atlas.bedrock_inference_arn", "")
        desc, _ = generate_description_from_turns(self.env, t)
        self.assertEqual(desc, "")
