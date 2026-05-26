"""Unit tests for the pure-Python OpenRouter client.

These tests do NOT use Odoo's TransactionCase — the module under test has no
ORM dependencies. The `@tagged` decorator lets `--test-tags t2av` pick it up.
"""

from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import BaseCase, tagged

from odoo.addons.t2av.services import openrouter_client
from odoo.addons.t2av.services.openrouter_client import (
    BASE_URL,
    OpenRouterAPIError,
    OpenRouterAuthError,
    OpenRouterRateLimitError,
    OpenRouterTimeoutError,
    OpenRouterValidationError,
    cancel_job,
    poll_status,
    submit_video,
)


def _mock_response(status_code, json_body=None, headers=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.headers = headers or {}
    if json_body is None:
        response.json.side_effect = ValueError("no body")
    else:
        response.json.return_value = json_body
    return response


@tagged("post_install", "-at_install", "t2av")
class TestOpenRouterClient(BaseCase):
    API_KEY = "test-api-key"

    def _submit_kwargs(self, **overrides):
        base = dict(
            prompt="a cat surfing",
            duration=5,
            resolution="720p",
            aspect_ratio="16:9",
        )
        base.update(overrides)
        return base

    @patch.object(openrouter_client, "requests")
    def test_submit_video_happy_path(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            202,
            {"id": "gen_abc", "polling_url": "https://openrouter.ai/api/v1/videos/gen_abc", "status": "pending"},
        )

        result = submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(
            result,
            {
                "id": "gen_abc",
                "polling_url": "https://openrouter.ai/api/v1/videos/gen_abc",
                "status": "pending",
            },
        )
        mock_requests.request.assert_called_once()
        call_args = mock_requests.request.call_args
        self.assertEqual(call_args.args[0], "POST")
        self.assertEqual(call_args.args[1], f"{BASE_URL}/videos")
        headers = call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], f"Bearer {self.API_KEY}")
        self.assertEqual(headers["Content-Type"], "application/json")
        body = call_args.kwargs["json"]
        self.assertEqual(body["prompt"], "a cat surfing")
        self.assertEqual(body["duration"], 5)
        self.assertEqual(body["resolution"], "720p")
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertEqual(body["model"], "bytedance/seedance-2.0")
        self.assertTrue(body["generate_audio"])

    @patch.object(openrouter_client, "requests")
    def test_submit_video_drops_none_kwargs(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            202, {"id": "gen_x", "polling_url": None, "status": "pending"}
        )

        submit_video(
            self.API_KEY,
            **self._submit_kwargs(seed=None, negative_prompt=None),
        )

        body = mock_requests.request.call_args.kwargs["json"]
        self.assertNotIn("seed", body)
        self.assertNotIn("negative_prompt", body)

    @patch.object(openrouter_client, "requests")
    def test_submit_video_auth_error(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            401, {"error": "invalid api key"}
        )

        with self.assertRaises(OpenRouterAuthError) as ctx:
            submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(ctx.exception.status_code, 401)
        # 401 must NOT retry — single call only.
        self.assertEqual(mock_requests.request.call_count, 1)

    @patch.object(openrouter_client, "requests")
    def test_submit_video_validation_error(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            400, {"error": "prompt too long"}
        )

        with self.assertRaises(OpenRouterValidationError) as ctx:
            submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.body, {"error": "prompt too long"})
        self.assertEqual(mock_requests.request.call_count, 1)

    @patch.object(openrouter_client, "time")
    @patch.object(openrouter_client, "requests")
    def test_submit_video_rate_limit_then_success(self, mock_requests, mock_time):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.side_effect = [
            _mock_response(429, {"error": "slow down"}, headers={"Retry-After": "1"}),
            _mock_response(202, {"id": "gen_ok", "polling_url": "u", "status": "pending"}),
        ]

        result = submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(result["id"], "gen_ok")
        self.assertEqual(mock_requests.request.call_count, 2)
        mock_time.sleep.assert_called()

    @patch.object(openrouter_client, "time")
    @patch.object(openrouter_client, "requests")
    def test_submit_video_rate_limit_exhausted(self, mock_requests, mock_time):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            429, {"error": "slow down"}, headers={"Retry-After": "1"}
        )

        with self.assertRaises(OpenRouterRateLimitError) as ctx:
            submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(mock_requests.request.call_count, 3)

    @patch.object(openrouter_client, "time")
    @patch.object(openrouter_client, "requests")
    def test_submit_video_5xx_then_success(self, mock_requests, mock_time):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.side_effect = [
            _mock_response(503, {"error": "down"}),
            _mock_response(202, {"id": "gen_ok", "polling_url": "u", "status": "pending"}),
        ]

        result = submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(result["id"], "gen_ok")
        self.assertEqual(mock_requests.request.call_count, 2)

    @patch.object(openrouter_client, "time")
    @patch.object(openrouter_client, "requests")
    def test_submit_video_5xx_exhausted(self, mock_requests, mock_time):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(503, {"error": "down"})

        with self.assertRaises(OpenRouterAPIError) as ctx:
            submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(mock_requests.request.call_count, 3)

    @patch.object(openrouter_client, "time")
    @patch.object(openrouter_client, "requests")
    def test_submit_video_timeout_exhausted(self, mock_requests, mock_time):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.side_effect = requests.Timeout("boom")

        with self.assertRaises(OpenRouterTimeoutError):
            submit_video(self.API_KEY, **self._submit_kwargs())

        self.assertEqual(mock_requests.request.call_count, 3)

    @patch.object(openrouter_client, "requests")
    def test_poll_status_uses_polling_url(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            200, {"id": "gen_abc", "status": "in_progress"}
        )

        result = poll_status(self.API_KEY, "gen_abc", polling_url="https://x/y")

        self.assertEqual(result, {"id": "gen_abc", "status": "in_progress"})
        call_args = mock_requests.request.call_args
        self.assertEqual(call_args.args[0], "GET")
        self.assertEqual(call_args.args[1], "https://x/y")

    @patch.object(openrouter_client, "requests")
    def test_poll_status_uses_default_url_without_polling_url(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(
            200, {"id": "gen_abc", "status": "in_progress"}
        )

        poll_status(self.API_KEY, "gen_abc")

        call_args = mock_requests.request.call_args
        self.assertEqual(call_args.args[0], "GET")
        self.assertEqual(call_args.args[1], f"{BASE_URL}/videos/gen_abc")

    @patch.object(openrouter_client, "requests")
    def test_cancel_job_404_returns_empty(self, mock_requests):
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.request.return_value = _mock_response(404, {"error": "not found"})

        result = cancel_job(self.API_KEY, "gen_missing")

        self.assertEqual(result, {})
        call_args = mock_requests.request.call_args
        self.assertEqual(call_args.args[0], "DELETE")
        self.assertEqual(call_args.args[1], f"{BASE_URL}/videos/gen_missing")
