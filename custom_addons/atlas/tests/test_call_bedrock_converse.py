import json
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.atlas.controllers.llm_assisst_qc import _call_bedrock_converse


def _mock_httpx_client_ctx(response_mock):
    """Build a MagicMock that emulates the httpx.Client context-manager protocol.

    Source code uses:  with httpx.Client(...) as client: resp = client.post(...)
    So patching httpx.Client must return an object whose __enter__ yields a
    client whose .post(...) returns the desired response mock.
    """
    client_instance = MagicMock()
    client_instance.post.return_value = response_mock
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client_instance)
    ctx.__exit__ = MagicMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, client_instance


@tagged("atlas", "atlas_bedrock", "post_install", "-at_install")
class TestCallBedrockConverse(TransactionCase):

    def _make_response(self, status_code=200, json_body=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text if text else (json.dumps(json_body) if json_body is not None else "")
        if json_body is not None:
            resp.json.return_value = json_body
        else:
            resp.json.side_effect = ValueError("No JSON object could be decoded")
        return resp

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_successful_converse_returns_text_and_usage(self, mock_client_cls):
        resp = self._make_response(
            status_code=200,
            json_body={
                "output": {"message": {"content": [{"text": "Hello world"}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
        )
        factory, _ = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        text, usage = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="sys", user_message="hi")
        self.assertEqual(text, "Hello world")
        self.assertEqual(usage.get("input_tokens"), 10)
        self.assertEqual(usage.get("output_tokens"), 5)

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_empty_content_returns_empty_string(self, mock_client_cls):
        resp = self._make_response(
            status_code=200,
            json_body={"output": {"message": {"content": []}}, "usage": {}},
        )
        factory, _ = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        text, _ = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertEqual(text, "")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_missing_usage_returns_zeroed_dict(self, mock_client_cls):
        resp = self._make_response(
            status_code=200,
            json_body={"output": {"message": {"content": [{"text": "ok"}]}}},
        )
        factory, _ = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        text, usage = _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertEqual(text, "ok")
        self.assertEqual(usage.get("input_tokens"), 0)
        self.assertEqual(usage.get("output_tokens"), 0)

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_network_exception_propagates(self, mock_client_cls):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=Exception("timeout"))
        ctx.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = ctx
        with self.assertRaises(Exception):
            _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_4xx_response_raises_runtime_error(self, mock_client_cls):
        resp = self._make_response(status_code=400, text='{"error":"bad request"}')
        factory, _ = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        with self.assertRaises(RuntimeError):
            _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_post_called_with_auth_header(self, mock_client_cls):
        resp = self._make_response(
            status_code=200,
            json_body={"output": {"message": {"content": [{"text": "x"}]}}, "usage": {}},
        )
        factory, client_instance = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        _call_bedrock_converse(
            api_key="mykey", inference_arn="arn:test", region="us-east-1",
            system_prompt="", user_message="hi")
        self.assertTrue(client_instance.post.called)
        headers = client_instance.post.call_args.kwargs.get("headers", {})
        self.assertIn("Authorization", headers)
        self.assertIn("mykey", headers["Authorization"])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_post_payload_contains_messages(self, mock_client_cls):
        resp = self._make_response(
            status_code=200,
            json_body={"output": {"message": {"content": [{"text": "x"}]}}, "usage": {}},
        )
        factory, client_instance = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        _call_bedrock_converse(
            api_key="k", inference_arn="arn:test", region="us-east-1",
            system_prompt="sys-prompt", user_message="user-msg")
        payload = client_instance.post.call_args.kwargs.get("json", {})
        self.assertIn("messages", payload)
        self.assertEqual(payload.get("system"), [{"text": "sys-prompt"}])

    @patch("odoo.addons.atlas.controllers.llm_assisst_qc.httpx.Client")
    def test_malformed_json_response_raises(self, mock_client_cls):
        resp = self._make_response(status_code=200, text="not json{")
        factory, _ = _mock_httpx_client_ctx(resp)
        mock_client_cls.side_effect = factory
        with self.assertRaises(Exception):
            _call_bedrock_converse(
                api_key="k", inference_arn="arn:test", region="us-east-1",
                system_prompt="", user_message="hi")
