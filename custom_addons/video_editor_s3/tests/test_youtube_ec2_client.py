# -*- coding: utf-8 -*-
from unittest.mock import patch, MagicMock

import requests

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..services import youtube_ec2_client


def _resp(status, body=b"", json_data=None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.text = body.decode("utf-8") if isinstance(body, bytes) else (body or "")
    r.content = body if isinstance(body, bytes) else body.encode("utf-8")
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no body")
    return r


@tagged("post_install", "-at_install", "video_editor_s3")
class TestYoutubeEc2Client(TransactionCase):

    _payload = {
        "tasker_id": "42",
        "yt_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "start_time": 10.0,
        "end_time": 20.0,
    }

    def test_happy_path_returns_parsed_json(self):
        with patch.object(requests, "post", return_value=_resp(202, b'{"ok":true}', {"ok": True})) as mock_post:
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {"ok": True})
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "http://1.2.3.4:8000/download")
        self.assertEqual(kwargs["json"], self._payload)

    def test_strips_trailing_slash_from_base_url(self):
        with patch.object(requests, "post", return_value=_resp(202)) as mock_post:
            youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000/",
                payload=self._payload,
            )
        self.assertEqual(mock_post.call_args[0][0], "http://1.2.3.4:8000/download")

    def test_empty_base_url_raises_user_error(self):
        with self.assertRaises(UserError):
            youtube_ec2_client.submit_youtube_job(base_url="", payload=self._payload)

    def test_non_retryable_4xx_raises_immediately(self):
        with patch.object(requests, "post", return_value=_resp(400, b"bad request")) as mock_post:
            with self.assertRaises(UserError):
                youtube_ec2_client.submit_youtube_job(
                    base_url="http://1.2.3.4:8000",
                    payload=self._payload,
                )
        self.assertEqual(mock_post.call_count, 1)

    def test_retry_then_success(self):
        responses = [_resp(502, b"bad gateway"), _resp(202, b'{"ok":true}', {"ok": True})]
        with patch.object(requests, "post", side_effect=responses), \
             patch.object(youtube_ec2_client.time, "sleep"):
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {"ok": True})

    def test_retries_exhausted_raises_user_error(self):
        with patch.object(requests, "post", return_value=_resp(503, b"unavailable")), \
             patch.object(youtube_ec2_client.time, "sleep"):
            with self.assertRaises(UserError):
                youtube_ec2_client.submit_youtube_job(
                    base_url="http://1.2.3.4:8000",
                    payload=self._payload,
                )

    def test_connection_error_retried(self):
        responses = [requests.ConnectionError("boom"), _resp(202, b'{"ok":true}', {"ok": True})]
        with patch.object(requests, "post", side_effect=responses), \
             patch.object(youtube_ec2_client.time, "sleep"):
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {"ok": True})

    def test_timeout_retried(self):
        responses = [requests.Timeout("slow"), _resp(202, b'{"ok":true}', {"ok": True})]
        with patch.object(requests, "post", side_effect=responses), \
             patch.object(youtube_ec2_client.time, "sleep"):
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {"ok": True})

    def test_empty_body_returns_empty_dict(self):
        with patch.object(requests, "post", return_value=_resp(202, b"")):
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {})

    def test_non_json_body_returns_empty_dict(self):
        with patch.object(requests, "post", return_value=_resp(202, b"plain text")):
            result = youtube_ec2_client.submit_youtube_job(
                base_url="http://1.2.3.4:8000",
                payload=self._payload,
            )
        self.assertEqual(result, {})
