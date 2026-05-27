"""Unit tests for the Gemini-multimodal review client.

These tests do NOT use Odoo's TransactionCase — the module under test has no
ORM dependencies. The `@tagged` decorator lets `--test-tags t2av` pick it up.
"""

from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import BaseCase, tagged

from odoo.addons.t2av.services import review_client
from odoo.addons.t2av.services.review_client import (
    DEFAULT_OPENROUTER_MAX_TOKENS,
    DEFAULT_OPENROUTER_MODEL_ID,
    DEFAULT_REASONING_EFFORT,
    OPENROUTER_API_URL,
    ReviewAuthError,
    ReviewError,
    _build_review_user_text,
    _extract_reasoning_text,
    _format_previous_failures,
    _looks_like_url_fetch_error,
    review,
)


_SAMPLE_REPORT = """## Summary
Looks fine.

## Prompt fidelity
PF-* all PASS.

## Generative defects
None observed.

## Technical and content gates
All gates PASS.

```json
{
  "verdict": "ACCEPT",
  "category": "human_activities",
  "style": "precise",
  "priority": "medium",
  "rendered": {
    "resolution": "1920x1080",
    "fps": 30,
    "duration_seconds": 5,
    "codec": "h264",
    "audio_codec": "aac",
    "audio_sample_rate_hz": 48000,
    "audio_channels": 2
  },
  "counts": {
    "fatal_fails": 0,
    "major_fails": 0,
    "minor_fails": 0,
    "unverifiable": 0
  },
  "findings": [],
  "regenerate_recommended": false,
  "human_review_required": false,
  "rebuilder_hint": ""
}
```
"""


def _mock_response(status_code, json_body=None, text=None, headers=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.headers = headers or {}
    response.text = text or ""
    if json_body is None:
        response.json.side_effect = ValueError("no body")
    else:
        response.json.return_value = json_body
    if status_code >= 400:
        def _raise():
            raise requests.HTTPError(f"HTTP {status_code}")
        response.raise_for_status.side_effect = _raise
    else:
        response.raise_for_status.return_value = None
    return response


def _openrouter_success_body(text=_SAMPLE_REPORT, reasoning=None, reasoning_details=None,
                             prompt_tokens=1200, completion_tokens=800, gen_id="gen_abc"):
    message = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if reasoning_details is not None:
        message["reasoning_details"] = reasoning_details
    return {
        "id": gen_id,
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


_REVIEW_KW = dict(
    provider="openrouter",
    openrouter_api_key="sk-or-test",
    model_id=DEFAULT_OPENROUTER_MODEL_ID,
    video_url="https://example.s3/video.mp4",
    enriched_prompt="A person waving at the camera. 1920x1080 at 30 fps, ...",
    category="human_activities",
    style="Precise",
    priority="Medium",
    duration_seconds=5.0,
    resolution="1920x1080",
)


@tagged("post_install", "-at_install", "t2av")
class TestReviewUserTurnBuilders(BaseCase):

    def test_user_text_has_all_required_fields(self):
        text = _build_review_user_text(
            enriched_prompt="hello world",
            category="human_activities",
            style="Precise",
            priority="Medium",
            duration_seconds=5.0,
            resolution="1920x1080",
        )
        self.assertIn("ENRICHED_PROMPT:\nhello world", text)
        self.assertIn("CATEGORY: human_activities", text)
        self.assertIn("STYLE: Precise", text)
        self.assertIn("PRIORITY: Medium", text)
        self.assertIn("DURATION_SECONDS: 5.0", text)
        self.assertIn("RESOLUTION: 1920x1080", text)
        self.assertIn("VIDEO: attached below.", text)
        self.assertNotIn("PREVIOUS_ATTEMPT_FAILURES", text)

    def test_previous_failures_block_renders_top_five(self):
        failures = [
            {"rule": f"PF-RULE-{i}", "severity": "MAJOR", "evidence": f"thing {i}"}
            for i in range(8)
        ]
        block = _format_previous_failures(failures)
        self.assertTrue(block.startswith("PREVIOUS_ATTEMPT_FAILURES:"))
        for i in range(5):
            self.assertIn(f"PF-RULE-{i}", block)
        for i in range(5, 8):
            self.assertNotIn(f"PF-RULE-{i}", block)
        self.assertIn("Address each failure above.", block)

    def test_previous_failures_truncates_long_evidence(self):
        failures = [{"rule": "PF-X", "severity": "FATAL", "evidence": "z" * 1000}]
        block = _format_previous_failures(failures)
        self.assertIn("PF-X", block)
        self.assertIn("...", block)
        self.assertLess(len(block), 600)

    def test_empty_previous_failures_returns_empty_string(self):
        self.assertEqual(_format_previous_failures(None), "")
        self.assertEqual(_format_previous_failures([]), "")


@tagged("post_install", "-at_install", "t2av")
class TestReviewClientPayloadShape(BaseCase):

    @patch.object(review_client.requests, "post")
    def test_openrouter_payload_shape_for_gemini(self, mock_post):
        mock_post.return_value = _mock_response(200, _openrouter_success_body())

        result = review(**_REVIEW_KW)

        self.assertEqual(result["verdict"], "ACCEPT")
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(result["input_tokens"], 1200)
        self.assertEqual(result["output_tokens"], 800)
        self.assertEqual(result["video_delivery"], "url")
        self.assertEqual(result["num_frames"], 0)

        mock_post.assert_called_once()
        call_url = mock_post.call_args.args[0]
        self.assertEqual(call_url, OPENROUTER_API_URL)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["model"], DEFAULT_OPENROUTER_MODEL_ID)
        self.assertEqual(body["max_tokens"], DEFAULT_OPENROUTER_MAX_TOKENS)
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["top_p"], 0.9)
        self.assertEqual(body["reasoning"], {"effort": DEFAULT_REASONING_EFFORT})

        messages = body["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].strip())
        self.assertEqual(messages[1]["role"], "user")
        content = messages[1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("ENRICHED_PROMPT", content[0]["text"])
        self.assertEqual(content[1]["type"], "video_url")
        self.assertEqual(content[1]["video_url"]["url"], "https://example.s3/video.mp4")

        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-or-test")
        self.assertEqual(headers["Content-Type"], "application/json")

    @patch.object(review_client.requests, "post")
    def test_previous_failures_injected_into_user_turn(self, mock_post):
        mock_post.return_value = _mock_response(200, _openrouter_success_body())

        review(
            previous_failures=[
                {"rule": "PF-AUDIO-PRESENCE", "severity": "FATAL", "evidence": "silence"},
                {"rule": "GV-HANDS", "severity": "MAJOR", "evidence": "six fingers"},
            ],
            **_REVIEW_KW,
        )

        body = mock_post.call_args.kwargs["json"]
        user_text = body["messages"][1]["content"][0]["text"]
        self.assertIn("PREVIOUS_ATTEMPT_FAILURES:", user_text)
        self.assertIn("PF-AUDIO-PRESENCE", user_text)
        self.assertIn("GV-HANDS", user_text)
        self.assertIn("silence", user_text)
        self.assertIn("six fingers", user_text)


@tagged("post_install", "-at_install", "t2av")
class TestReviewClientReasoningCapture(BaseCase):

    @patch.object(review_client.requests, "post")
    def test_reasoning_field_string(self, mock_post):
        mock_post.return_value = _mock_response(
            200,
            _openrouter_success_body(reasoning="I considered the audio block carefully."),
        )

        result = review(**_REVIEW_KW)
        self.assertEqual(
            result["reasoning_text"], "I considered the audio block carefully."
        )

    @patch.object(review_client.requests, "post")
    def test_reasoning_details_list(self, mock_post):
        mock_post.return_value = _mock_response(
            200,
            _openrouter_success_body(
                reasoning_details=[
                    {"type": "reasoning.text", "text": "step one"},
                    {"type": "reasoning.text", "text": "step two"},
                ],
            ),
        )

        result = review(**_REVIEW_KW)
        self.assertIn("step one", result["reasoning_text"])
        self.assertIn("step two", result["reasoning_text"])

    @patch.object(review_client.requests, "post")
    def test_reasoning_missing_yields_empty_string(self, mock_post):
        mock_post.return_value = _mock_response(200, _openrouter_success_body())
        result = review(**_REVIEW_KW)
        self.assertEqual(result["reasoning_text"], "")

    def test_extract_reasoning_direct(self):
        self.assertEqual(_extract_reasoning_text({"reasoning": "abc"}), "abc")
        self.assertEqual(
            _extract_reasoning_text({"reasoning_details": [{"text": "x"}, {"text": "y"}]}),
            "x\ny",
        )
        self.assertEqual(_extract_reasoning_text({}), "")


@tagged("post_install", "-at_install", "t2av")
class TestReviewClientURLFetchFallback(BaseCase):

    def test_looks_like_url_fetch_error_detection(self):
        self.assertTrue(_looks_like_url_fetch_error(400, "could not fetch video url"))
        self.assertTrue(_looks_like_url_fetch_error(422, "media download failed"))
        self.assertFalse(_looks_like_url_fetch_error(400, "prompt too long"))
        self.assertFalse(_looks_like_url_fetch_error(500, "fetch failed"))
        self.assertFalse(_looks_like_url_fetch_error(200, "media url"))

    @patch.object(review_client, "_download_video_bytes")
    @patch.object(review_client.requests, "post")
    def test_url_fetch_error_triggers_base64_fallback(self, mock_post, mock_download):
        mock_download.return_value = b"\x00\x01\x02FAKEMP4"
        mock_post.side_effect = [
            _mock_response(
                400,
                json_body={"error": "could not fetch video url"},
                text='{"error": "could not fetch video url"}',
            ),
            _mock_response(200, _openrouter_success_body()),
        ]

        result = review(**_REVIEW_KW)
        self.assertEqual(result["verdict"], "ACCEPT")
        self.assertEqual(result["video_delivery"], "base64")
        self.assertEqual(mock_post.call_count, 2)
        mock_download.assert_called_once_with("https://example.s3/video.mp4")

        first_body = mock_post.call_args_list[0].kwargs["json"]
        second_body = mock_post.call_args_list[1].kwargs["json"]
        first_url = first_body["messages"][1]["content"][1]["video_url"]["url"]
        second_url = second_body["messages"][1]["content"][1]["video_url"]["url"]
        self.assertTrue(first_url.startswith("https://"))
        self.assertTrue(second_url.startswith("data:video/mp4;base64,"))

    @patch.object(review_client.requests, "post")
    def test_auth_failure_raises_immediately_no_fallback(self, mock_post):
        mock_post.return_value = _mock_response(
            401, json_body={"error": "bad key"}, text='{"error": "bad key"}'
        )
        with self.assertRaises(ReviewAuthError):
            review(**_REVIEW_KW)
        self.assertEqual(mock_post.call_count, 1)

    @patch.object(review_client.time, "sleep")
    @patch.object(review_client.requests, "post")
    def test_rate_limit_retries_then_succeeds(self, mock_post, _sleep):
        mock_post.side_effect = [
            _mock_response(429, json_body={"error": "rate"}, text='{"error":"rate"}'),
            _mock_response(200, _openrouter_success_body()),
        ]
        result = review(**_REVIEW_KW)
        self.assertEqual(result["verdict"], "ACCEPT")
        self.assertEqual(mock_post.call_count, 2)

    @patch.object(review_client.time, "sleep")
    @patch.object(review_client.requests, "post")
    def test_5xx_exhausted_raises_review_error(self, mock_post, _sleep):
        mock_post.return_value = _mock_response(
            503, json_body={"error": "down"}, text='{"error":"down"}'
        )
        with self.assertRaises(ReviewError):
            review(max_attempts=2, **_REVIEW_KW)
        self.assertEqual(mock_post.call_count, 2)

    @patch.object(review_client.requests, "post")
    def test_empty_content_raises_review_error(self, mock_post):
        mock_post.return_value = _mock_response(200, _openrouter_success_body(text=""))
        with self.assertRaises(ReviewError) as ctx:
            review(**_REVIEW_KW)
        self.assertIn("empty text", str(ctx.exception))


@tagged("post_install", "-at_install", "t2av")
class TestReviewEntryPointDispatch(BaseCase):

    def test_unknown_provider_raises(self):
        with self.assertRaises(review_client.ReviewConfigError):
            review(
                provider="kimi",
                openrouter_api_key="x",
                model_id="anything",
                video_url="https://x/y.mp4",
                enriched_prompt="abc",
                category="human_activities",
                style="Precise",
                priority="Medium",
                duration_seconds=5.0,
                resolution="1920x1080",
            )

    def test_missing_video_url_raises(self):
        with self.assertRaises(ReviewError):
            kwargs = dict(_REVIEW_KW)
            kwargs["video_url"] = ""
            review(**kwargs)

    def test_missing_enriched_prompt_raises(self):
        with self.assertRaises(ReviewError):
            kwargs = dict(_REVIEW_KW)
            kwargs["enriched_prompt"] = ""
            review(**kwargs)
