# -*- coding: utf-8 -*-
import hashlib
import io
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "crowley_ai_vid_gen")
class TestOpenRouterClient(TransactionCase):

    def setUp(self):
        super().setUp()
        self.client = self.env["crowley.ai.vid.gen.openrouter.client"]
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("crowley_ai_vid_gen.openrouter_api_key", "sk-or-v1-test")

    def test_get_api_key_raises_when_missing(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "crowley_ai_vid_gen.openrouter_api_key", "")
        with self.assertRaises(UserError):
            self.client._get_api_key()

    def test_get_headers_includes_auth(self):
        headers = self.client._get_headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-or-v1-test")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_get_headers_no_content_type(self):
        headers = self.client._get_headers(include_content_type=False)
        self.assertNotIn("Content-Type", headers)

    def test_get_headers_optional_attribution(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("crowley_ai_vid_gen.openrouter_http_referer", "https://test.example")
        ICP.set_param("crowley_ai_vid_gen.openrouter_app_title", "Test App")
        h = self.client._get_headers()
        self.assertEqual(h["HTTP-Referer"], "https://test.example")
        self.assertEqual(h["X-OpenRouter-Title"], "Test App")

    @patch("odoo.addons.crowley_ai_vid_gen.services.openrouter_client.requests.post")
    def test_submit_video_job_happy_path(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 202
        mock_resp.json.return_value = {
            "id": "abc123",
            "polling_url": "https://openrouter.ai/api/v1/videos/abc123",
            "status": "pending",
        }
        mock_post.return_value = mock_resp
        result = self.client.submit_video_job(
            model="bytedance/seedance-2.0",
            prompt="test",
            duration=5,
            resolution="720p",
            aspect_ratio="16:9",
        )
        self.assertEqual(result["id"], "abc123")
        # Verify the request body was correct
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["model"], "bytedance/seedance-2.0")
        self.assertEqual(kwargs["json"]["duration"], 5)
        self.assertEqual(kwargs["json"]["generate_audio"], True)

    @patch("odoo.addons.crowley_ai_vid_gen.services.openrouter_client.requests.post")
    def test_submit_video_job_missing_polling_url_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"id": "abc", "status": "pending"}  # no polling_url
        mock_post.return_value = mock_resp
        with self.assertRaises(UserError):
            self.client.submit_video_job(
                model="bytedance/seedance-2.0",
                prompt="x", duration=5, resolution="720p", aspect_ratio="16:9",
            )

    @patch("odoo.addons.crowley_ai_vid_gen.services.openrouter_client.requests.post")
    def test_submit_video_job_402_credits(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 402
        mock_resp.text = "insufficient credits"
        mock_post.return_value = mock_resp
        with self.assertRaises(UserError) as ctx:
            self.client.submit_video_job(
                model="bytedance/seedance-2.0",
                prompt="x", duration=5, resolution="720p", aspect_ratio="16:9",
            )
        self.assertIn("credits", str(ctx.exception).lower())

    @patch("odoo.addons.crowley_ai_vid_gen.services.openrouter_client.requests.post")
    def test_submit_video_job_429_rate_limit(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 429
        mock_resp.text = "rate limited"
        mock_resp.headers = {"Retry-After": "60"}
        mock_post.return_value = mock_resp
        with self.assertRaises(UserError) as ctx:
            self.client.submit_video_job(
                model="bytedance/seedance-2.0",
                prompt="x", duration=5, resolution="720p", aspect_ratio="16:9",
            )
        self.assertIn("60", str(ctx.exception))

    def test_poll_job_no_url_raises(self):
        with self.assertRaises(UserError):
            self.client.poll_job("")

    @patch("odoo.addons.crowley_ai_vid_gen.services.openrouter_client.requests.get")
    def test_poll_job_in_flight(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "in_progress"}
        mock_get.return_value = mock_resp
        result = self.client.poll_job("https://openrouter.ai/api/v1/videos/abc")
        self.assertEqual(result["status"], "in_progress")
