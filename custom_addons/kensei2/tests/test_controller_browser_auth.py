# -*- coding: utf-8 -*-
import base64
import json
from unittest.mock import patch, MagicMock, PropertyMock

import requests

from odoo.tests import tagged

from .common import Kensei2TestCase

_BROWSER_MOD = "odoo.addons.kensei2.controllers.browser_auth"


@tagged("post_install", "-at_install")
class TestBrowserApiBaseUrl(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController

    def test_url_local_mode(self):
        ctrl_cls = self._ctrl()
        self.claude_sandbox.write({"docker_port": 21222})
        with patch.object(
            type(self.claude_sandbox), "_deployment_mode", return_value="local",
        ):
            url = ctrl_cls._browser_api_base(self.claude_sandbox)
        self.assertEqual(url, "http://localhost:21222/browser-api")

    def test_url_k8s_mode_with_ws_host(self):
        ctrl_cls = self._ctrl()
        self._set_param("kensei2.ws_router_host", "ws.ethara.ai")
        with patch.object(
            type(self.claude_sandbox), "_deployment_mode", return_value="k8s",
        ):
            url = ctrl_cls._browser_api_base(self.claude_sandbox)
        expected = "https://ws.ethara.ai/sandbox/%s/browser-api" % self.claude_sandbox.id
        self.assertEqual(url, expected)

    def test_url_k8s_mode_no_ws_host(self):
        ctrl_cls = self._ctrl()
        self._set_param("kensei2.ws_router_host", "")
        with patch.object(
            type(self.claude_sandbox), "_deployment_mode", return_value="k8s",
        ):
            url = ctrl_cls._browser_api_base(self.claude_sandbox)
        svc_name = "kensei2-sandbox-%s" % self.claude_sandbox.id
        expected = "http://%s.kensei2.svc.cluster.local:18789/browser-api" % svc_name
        self.assertEqual(url, expected)


@tagged("post_install", "-at_install")
class TestBrowserScreenshot(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_success_image_content(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.content = b"PNG_IMAGE_DATA"
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("image", result)
        self.assertEqual(
            base64.b64decode(result["image"]),
            b"PNG_IMAGE_DATA",
        )

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_success_json_content(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {"image": "base64data"}
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertEqual(result["image"], "base64data")

    def test_screenshot_not_running(self):
        self.claude_sandbox.write({"docker_status": "stopped"})
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("not running", result["error"])

    def test_screenshot_no_api_url(self):
        self.claude_sandbox.write({"docker_status": "running", "docker_port": 0})
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_http_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("500", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_timeout(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = requests.Timeout("timed out")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_connection_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = requests.ConnectionError("refused")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("connect", result["error"].lower())


@tagged("post_install", "-at_install")
class TestBrowserInjectCookies(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_list(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        cookies = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id, cookies=cookies,
                )
        self.assertTrue(result.get("success"))
        self.assertEqual(result["count"], 2)

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_string_semicolon(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id, cookies="a=1; b=2",
                )
        self.assertTrue(result.get("success"))
        self.assertEqual(result["count"], 2)

    def test_inject_cookies_empty(self):
        self._setup_running_sandbox()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id, cookies=None,
                )
        self.assertIn("error", result)

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_with_url(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id,
                    cookies="sid=abc",
                    url="https://example.com",
                )
        self.assertTrue(result.get("success"))
        call_args = mock_requests.post.call_args
        sent_cookies = call_args[1]["json"]["cookies"]
        self.assertEqual(sent_cookies[0]["url"], "https://example.com")

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_http_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id,
                    cookies=[{"name": "a", "value": "1"}],
                )
        self.assertIn("error", result)


@tagged("post_install", "-at_install")
class TestBrowserStatus(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    @patch(_BROWSER_MOD + ".requests")
    def test_status_success(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"url": "https://example.com", "title": "Example"}]
        mock_requests.get.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("tabs", result)

    def test_status_not_running(self):
        self.claude_sandbox.write({"docker_status": "stopped"})
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)

    @patch(_BROWSER_MOD + ".requests")
    def test_status_timeout(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.get.side_effect = requests.Timeout("timed out")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])


# ═══════════════════════════════════════════════════════════════════════
#  Error-path & edge-case tests (appended)
# ═══════════════════════════════════════════════════════════════════════

@tagged("post_install", "-at_install")
class TestBrowserScreenshotErrorPaths(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_unexpected_json(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {"status": "ok"}
        mock_requests.post.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("Unexpected", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_screenshot_generic_exception(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = RuntimeError("something broke")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_screenshot(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("something broke", result["error"])


@tagged("post_install", "-at_install")
class TestBrowserInjectCookiesErrorPaths(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_empty_string(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id, cookies="",
                )
        self.assertIn("error", result)

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_timeout(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = requests.Timeout("timed out")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id,
                    cookies=[{"name": "a", "value": "1"}],
                )
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_connection_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = requests.ConnectionError("refused")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id,
                    cookies=[{"name": "a", "value": "1"}],
                )
        self.assertIn("error", result)
        self.assertIn("connect", result["error"].lower())

    @patch(_BROWSER_MOD + ".requests")
    def test_inject_cookies_generic_exception(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.post.side_effect = RuntimeError("unexpected")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id,
                    cookies=[{"name": "a", "value": "1"}],
                )
        self.assertIn("error", result)
        self.assertIn("unexpected", result["error"])

    def test_inject_cookies_string_without_equals(self):
        self._setup_running_sandbox()
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.inject_cookies(
                    sandbox_id=self.claude_sandbox.id, cookies="nopairs",
                )
        self.assertIn("error", result)
        self.assertIn("No valid cookies", result["error"])


@tagged("post_install", "-at_install")
class TestBrowserStatusErrorPaths(Kensei2TestCase):

    def _ctrl(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController()

    def _setup_running_sandbox(self):
        self.claude_sandbox.write({
            "docker_status": "running",
            "docker_port": 21999,
        })

    def test_status_missing_sandbox_id(self):
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            result = ctrl.browser_status(sandbox_id=0)
        self.assertIn("error", result)
        self.assertIn("required", result["error"])

    def test_status_no_api_url(self):
        self.claude_sandbox.write({"docker_status": "running", "docker_port": 0})
        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("API URL", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_status_http_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_requests.get.return_value = mock_resp
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("500", result["error"])

    @patch(_BROWSER_MOD + ".requests")
    def test_status_connection_error(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.get.side_effect = requests.ConnectionError("refused")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("connect", result["error"].lower())

    @patch(_BROWSER_MOD + ".requests")
    def test_status_generic_exception(self, mock_requests):
        self._setup_running_sandbox()
        mock_requests.get.side_effect = RuntimeError("boom")
        mock_requests.Timeout = requests.Timeout
        mock_requests.ConnectionError = requests.ConnectionError

        ctrl = self._ctrl()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            with patch.object(
                type(self.claude_sandbox), "_deployment_mode", return_value="local",
            ):
                result = ctrl.browser_status(sandbox_id=self.claude_sandbox.id)
        self.assertIn("error", result)
        self.assertIn("boom", result["error"])


@tagged("post_install", "-at_install")
class TestBrowserValidateAndApiBaseEdgeCases(Kensei2TestCase):

    def _ctrl_cls(self):
        from odoo.addons.kensei2.controllers.browser_auth import Kensei2BrowserAuthController
        return Kensei2BrowserAuthController

    def test_browser_api_base_local_no_port(self):
        self.claude_sandbox.write({"docker_port": 0})
        ctrl_cls = self._ctrl_cls()
        with patch.object(
            type(self.claude_sandbox), "_deployment_mode", return_value="local",
        ):
            url = ctrl_cls._browser_api_base(self.claude_sandbox)
        self.assertIsNone(url)

    def test_validate_sandbox_missing_id(self):
        ctrl_cls = self._ctrl_cls()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            sandbox, err = ctrl_cls._validate_sandbox(0)
        self.assertIsNone(sandbox)
        self.assertIn("error", err)
        self.assertIn("required", err["error"])

    def test_validate_sandbox_not_found(self):
        ctrl_cls = self._ctrl_cls()
        with patch("odoo.http.request") as mock_req:
            mock_req.env = self.env
            sandbox, err = ctrl_cls._validate_sandbox(999999)
        self.assertIsNone(sandbox)
        self.assertIn("error", err)
        self.assertIn("Sandbox not found", err["error"])
