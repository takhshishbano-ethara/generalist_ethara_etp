# -*- coding: utf-8 -*-
import base64
import json
import logging

import requests

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_BROWSER_API_TIMEOUT = 15


class KenseiBrowserAuthController(http.Controller):

    @staticmethod
    def _browser_api_base(sandbox):
        mode = sandbox._deployment_mode()
        if mode == "k8s":
            ws_host = (
                sandbox.env["ir.config_parameter"]
                .sudo()
                .get_param("kensei.ws_router_host", "")
                .strip()
            )
            if ws_host:
                return "https://%s/sandbox/%s/browser-api" % (ws_host, sandbox.id)
            svc_name = "kensei-sandbox-%s" % sandbox.id
            return (
                "http://%s.kensei.svc.cluster.local:18789/browser-api"
                % svc_name
            )
        if sandbox.docker_port:
            return "http://localhost:%d/browser-api" % sandbox.docker_port
        return None

    @staticmethod
    def _validate_sandbox(sandbox_id):
        sandbox_id = int(sandbox_id or 0)
        if not sandbox_id:
            return None, {"error": "sandbox_id is required"}
        sandbox = request.env["kensei.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return None, {"error": "Sandbox not found"}
        if sandbox.docker_status != "running":
            return None, {"error": "Sandbox is not running"}
        return sandbox, None

    @http.route("/kensei/browser/screenshot", type="json", auth="user")
    def browser_screenshot(self, sandbox_id=0, **kw):
        sandbox, err = self._validate_sandbox(sandbox_id)
        if err:
            return err

        base_url = self._browser_api_base(sandbox)
        if not base_url:
            return {"error": "Cannot determine browser API URL"}

        try:
            resp = requests.post(
                "%s/screenshot" % base_url,
                json={"fullPage": False},
                timeout=_BROWSER_API_TIMEOUT,
            )
            if resp.status_code != 200:
                _logger.warning(
                    "Browser screenshot HTTP %s: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                return {"error": "Screenshot failed (HTTP %s)" % resp.status_code}

            content_type = resp.headers.get("Content-Type", "")
            if "image" in content_type:
                return {"image": base64.b64encode(resp.content).decode()}

            try:
                data = resp.json()
                if data.get("image"):
                    return {"image": data["image"]}
                if data.get("screenshot"):
                    return {"image": data["screenshot"]}
                return {"error": "Unexpected screenshot response format"}
            except (ValueError, KeyError):
                return {"image": base64.b64encode(resp.content).decode()}

        except requests.Timeout:
            return {"error": "Screenshot request timed out"}
        except requests.ConnectionError:
            return {"error": "Cannot connect to browser API"}
        except Exception as exc:
            _logger.exception("Browser screenshot failed")
            return {"error": str(exc)}

    @http.route("/kensei/browser/inject_cookies", type="json", auth="user")
    def inject_cookies(self, sandbox_id=0, cookies=None, url=None, **kw):
        sandbox, err = self._validate_sandbox(sandbox_id)
        if err:
            return err

        base_url = self._browser_api_base(sandbox)
        if not base_url:
            return {"error": "Cannot determine browser API URL"}

        if not cookies:
            return {"error": "cookies parameter is required"}

        if isinstance(cookies, str):
            parsed = []
            for pair in cookies.split(";"):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                name, _, value = pair.partition("=")
                cookie = {"name": name.strip(), "value": value.strip()}
                if url:
                    cookie["url"] = url
                parsed.append(cookie)
            cookies = parsed

        if not isinstance(cookies, list) or len(cookies) == 0:
            return {"error": "No valid cookies provided"}

        try:
            resp = requests.post(
                "%s/cookies/set" % base_url,
                json={"cookies": cookies},
                timeout=_BROWSER_API_TIMEOUT,
            )
            if resp.status_code != 200:
                _logger.warning(
                    "Cookie inject HTTP %s: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                return {"error": "Cookie injection failed (HTTP %s)" % resp.status_code}
            return {"success": True, "count": len(cookies)}
        except requests.Timeout:
            return {"error": "Cookie inject request timed out"}
        except requests.ConnectionError:
            return {"error": "Cannot connect to browser API"}
        except Exception as exc:
            _logger.exception("Cookie injection failed")
            return {"error": str(exc)}

    @http.route("/kensei/browser/status", type="json", auth="user")
    def browser_status(self, sandbox_id=0, **kw):
        sandbox, err = self._validate_sandbox(sandbox_id)
        if err:
            return err

        base_url = self._browser_api_base(sandbox)
        if not base_url:
            return {"error": "Cannot determine browser API URL"}

        try:
            resp = requests.get(
                "%s/tabs" % base_url,
                timeout=_BROWSER_API_TIMEOUT,
            )
            if resp.status_code != 200:
                return {"error": "Browser status failed (HTTP %s)" % resp.status_code}
            return {"tabs": resp.json()}
        except requests.Timeout:
            return {"error": "Browser status request timed out"}
        except requests.ConnectionError:
            return {"error": "Cannot connect to browser API"}
        except Exception as exc:
            _logger.exception("Browser status failed")
            return {"error": str(exc)}
