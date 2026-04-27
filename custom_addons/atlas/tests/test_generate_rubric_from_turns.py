from unittest.mock import patch
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.addons.atlas.models.atlas import generate_rubric_from_turns


@tagged("atlas", "atlas_rubgen", "post_install", "-at_install")
class TestGenerateRubricFromTurns(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.task = cls.env["atlas.atlas"].create({})
        cls.sbx = cls.env["atlas.sandbox"].create(
            {"atlas_id": cls.task.id, "model_type": "glm"})
        cls.Turn = cls.env["atlas.turn"]

    def test_no_turns_returns_empty_list(self):
        r, u = generate_rubric_from_turns(self.env, self.Turn.browse())
        self.assertEqual(r, [])
        self.assertEqual(u, {})

    def test_missing_creds_returns_empty(self):
        self.env["ir.config_parameter"].sudo().set_param("atlas.bedrock_inference_arn", "")
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "p", "response": "r",
                              "turn_status": "Completed"})
        r, u = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertEqual(r, [])

    def test_no_sent_turns_returns_empty(self):
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "", "turn_status": "Pending"})
        r, _ = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertEqual(r, [])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse")
    def test_bedrock_returns_markdown_parsed(self, mock_call):
        mock_call.return_value = (
            "```markdown\n| Rule statement passes length minimum here now | task_completion | important | 0: x |\n```",
            {"input_tokens": 5, "output_tokens": 3})
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "x"
        self.env["ir.config_parameter"].sudo().set_param("atlas.bedrock_inference_arn", "arn:fake")
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "p", "response": "r",
                              "turn_status": "Completed"})
        r, u = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertTrue(isinstance(r, list))

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse",
           side_effect=Exception("net"))
    def test_exception_returns_empty(self, _):
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "x"
        self.env["ir.config_parameter"].sudo().set_param("atlas.bedrock_inference_arn", "arn:fake")
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "p", "response": "r",
                              "turn_status": "Completed"})
        r, u = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertEqual(r, [])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse")
    def test_empty_response_returns_empty_rubric(self, mock_call):
        mock_call.return_value = ("", {"input_tokens": 0, "output_tokens": 0})
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "x"
        self.env["ir.config_parameter"].sudo().set_param("atlas.bedrock_inference_arn", "arn:fake")
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "p", "response": "r",
                              "turn_status": "Completed"})
        r, _ = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertEqual(r, [])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc._call_bedrock_converse")
    def test_non_markdown_response_empty_parse(self, mock_call):
        mock_call.return_value = ("Just plain prose with no pipes here.", {"input_tokens": 1, "output_tokens": 1})
        import os
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "x"
        self.env["ir.config_parameter"].sudo().set_param("atlas.bedrock_inference_arn", "arn:fake")
        t = self.Turn.create({"sandbox_id": self.sbx.id, "prompt": "p", "response": "r",
                              "turn_status": "Completed"})
        r, _ = generate_rubric_from_turns(self.env, t, task_id=self.task.id)
        self.assertEqual(r, [])
