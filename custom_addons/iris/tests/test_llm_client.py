"""Tests for ``services/llm_client.py`` with ``requests`` fully mocked."""

from unittest.mock import patch

import requests

from odoo.tests.common import tagged

from .common import IrisCase
from odoo.addons.iris.services import llm_client

#: ``requests.request`` as resolved inside the llm_client module.
REQUEST_TARGET = "odoo.addons.iris.services.llm_client.requests.request"
#: Avoid real backoff sleeps in retry tests.
SLEEP_TARGET = "odoo.addons.iris.services.llm_client.time.sleep"

OK_BODY = {
    "choices": [{"message": {"content": "# Screening Record — Jane Doe"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 80, "cost": 0.0042},
    "model": "anthropic/claude-sonnet-4.5",
}


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            msg = "no JSON body"
            raise ValueError(msg)
        return self._body


def _call(**overrides):
    """Invoke chat_completion with test defaults (3 retries, no referer)."""
    kwargs = {
        "model": "test-model",
        "system_prompt": "SYSTEM",
        "user_text": "USER",
        "max_retries": 3,
    }
    kwargs.update(overrides)
    return llm_client.chat_completion("sk-test", **kwargs)


@tagged("post_install", "-at_install", "iris")
class TestLlmClient(IrisCase):
    def test_200_happy_path_parses_usage(self):
        with patch(REQUEST_TARGET, return_value=FakeResponse(200, OK_BODY)) as req:
            result = _call()

        self.assertEqual(result["content"], "# Screening Record — Jane Doe")
        self.assertEqual(result["prompt_tokens"], 120)
        self.assertEqual(result["completion_tokens"], 80)
        self.assertEqual(result["cost_usd"], 0.0042)
        self.assertEqual(result["model"], "anthropic/claude-sonnet-4.5")
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(result["raw"], OK_BODY)
        self.assertEqual(req.call_count, 1)

        # Payload shape: system + user messages and the usage.include flag.
        _, kwargs = req.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["usage"], {"include": True})
        self.assertEqual(
            [m["role"] for m in payload["messages"]], ["system", "user"],
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer sk-test",
        )

    def test_missing_cost_yields_none(self):
        body = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with patch(REQUEST_TARGET, return_value=FakeResponse(200, body)):
            result = _call()
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["model"], "test-model")  # falls back to input

    def test_429_then_200_retries(self):
        responses = [
            FakeResponse(429, {"error": "rate limited"}, {"Retry-After": "1"}),
            FakeResponse(200, OK_BODY),
        ]
        with patch(REQUEST_TARGET, side_effect=responses) as req, \
                patch(SLEEP_TARGET) as sleep:
            result = _call()
        self.assertEqual(result["content"], "# Screening Record — Jane Doe")
        self.assertEqual(req.call_count, 2)
        sleep.assert_called_once_with(1)  # honours Retry-After (capped at 60)

    def test_429_exhaustion_raises_rate_limit_error(self):
        responses = [FakeResponse(429, {"error": "rate"}, {"Retry-After": "2"})] * 3
        with patch(REQUEST_TARGET, side_effect=responses) as req, \
                patch(SLEEP_TARGET):
            with self.assertRaises(llm_client.LLMRateLimitError) as ctx:
                _call()
        self.assertEqual(req.call_count, 3)
        self.assertEqual(ctx.exception.retry_after, 2)

    def test_401_raises_auth_error_without_retry(self):
        response = FakeResponse(401, {"error": {"message": "bad key"}})
        with patch(REQUEST_TARGET, return_value=response) as req, \
                patch(SLEEP_TARGET) as sleep:
            with self.assertRaises(llm_client.LLMAuthError) as ctx:
                _call()
        self.assertEqual(req.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_403_raises_auth_error_without_retry(self):
        with patch(REQUEST_TARGET, return_value=FakeResponse(403, {})) as req:
            with self.assertRaises(llm_client.LLMAuthError):
                _call()
        self.assertEqual(req.call_count, 1)

    def test_400_raises_api_error_without_retry(self):
        response = FakeResponse(400, {"error": "bad request"})
        with patch(REQUEST_TARGET, return_value=response) as req:
            with self.assertRaises(llm_client.LLMAPIError):
                _call()
        self.assertEqual(req.call_count, 1)

    def test_timeout_exhaustion_raises_timeout_error(self):
        with patch(
            REQUEST_TARGET, side_effect=requests.Timeout("connect timed out"),
        ) as req, patch(SLEEP_TARGET):
            with self.assertRaises(llm_client.LLMTimeoutError):
                _call()
        self.assertEqual(req.call_count, 3)

    def test_connection_error_then_200_recovers(self):
        side_effects = [
            requests.ConnectionError("reset by peer"),
            FakeResponse(200, OK_BODY),
        ]
        with patch(REQUEST_TARGET, side_effect=side_effects) as req, \
                patch(SLEEP_TARGET):
            result = _call()
        self.assertEqual(req.call_count, 2)
        self.assertEqual(result["prompt_tokens"], 120)

    def test_5xx_retry_then_exhaustion(self):
        responses = [FakeResponse(503, {"error": "overloaded"})] * 3
        with patch(REQUEST_TARGET, side_effect=responses) as req, \
                patch(SLEEP_TARGET):
            with self.assertRaises(llm_client.LLMAPIError) as ctx:
                _call()
        self.assertEqual(req.call_count, 3)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_empty_content_raises_api_error(self):
        body = {"choices": [{"message": {"content": "   "}}], "usage": {}}
        with patch(REQUEST_TARGET, return_value=FakeResponse(200, body)):
            with self.assertRaises(llm_client.LLMAPIError):
                _call()

    def test_malformed_body_raises_api_error(self):
        with patch(REQUEST_TARGET, return_value=FakeResponse(200, {"choices": []})):
            with self.assertRaises(llm_client.LLMAPIError):
                _call()

    def test_base_url_trailing_slash_normalised(self):
        with patch(REQUEST_TARGET, return_value=FakeResponse(200, OK_BODY)) as req:
            _call(base_url="https://gateway.example.com/v1/")
        args, _ = req.call_args
        self.assertEqual(args[1], "https://gateway.example.com/v1/chat/completions")
