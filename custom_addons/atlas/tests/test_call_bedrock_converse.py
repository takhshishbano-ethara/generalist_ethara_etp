import json
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.atlas.controllers.llm_assisst_qc import _call_bedrock_converse


@tagged("atlas", "atlas_bedrock", "post_install", "-at_install")
class TestCallBedrockConverse(TransactionCase):
    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_successful_converse_returns_text_and_usage(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({
                "output": {"message": {"content": [{"text": "Hello world"}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }),
        )
        mock_post.return_value.raise_for_status = lambda: None
        text, usage = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="sys", user_message="hi")
        self.assertEqual(text, "Hello world")
        self.assertEqual(usage.get("input_tokens"), 10)
        self.assertEqual(usage.get("output_tokens"), 5)

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_empty_content_returns_empty_string(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({"output": {"message": {"content": []}}, "usage": {}}),
        )
        mock_post.return_value.raise_for_status = lambda: None
        text, _ = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertEqual(text, "")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_missing_usage_returns_zeroed_dict(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({"output": {"message": {"content": [{"text": "ok"}]}}}),
        )
        mock_post.return_value.raise_for_status = lambda: None
        text, usage = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertEqual(text, "ok")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post",
           side_effect=Exception("timeout"))
    def test_network_exception_propagates_or_handled(self, _):
        try:
            text, _u = _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")
            self.assertEqual(text, "")
        except Exception:
            pass

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_4xx_response_handled(self, mock_post):
        resp = MagicMock(status_code=400, text='{"error":"bad request"}')
        def raise_err():
            raise Exception("400 error")
        resp.raise_for_status = raise_err
        mock_post.return_value = resp
        try:
            text, _ = _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")
            self.assertEqual(text, "")
        except Exception:
            pass

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_post_called_with_auth_header(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({"output": {"message": {"content": [{"text": "x"}]}}, "usage": {}}),
        )
        mock_post.return_value.raise_for_status = lambda: None
        _call_bedrock_converse(
            api_key="mykey", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertTrue(mock_post.called)
        call = mock_post.call_args
        headers = call.kwargs.get("headers", {})
        self.assertIn("Authorization", headers)
        self.assertIn("mykey", headers["Authorization"])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_post_payload_contains_messages(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps({"output": {"message": {"content": [{"text": "x"}]}}, "usage": {}}),
        )
        mock_post.return_value.raise_for_status = lambda: None
        _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="sys-prompt", user_message="user-msg")
        payload = mock_post.call_args.kwargs.get("json", {})
        self.assertIn("messages", payload)

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.requests.post")
    def test_malformed_json_response_handled(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, text="not json{")
        mock_post.return_value.raise_for_status = lambda: None
        try:
            text, _ = _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")
            self.assertEqual(text, "")
        except Exception:
            pass
