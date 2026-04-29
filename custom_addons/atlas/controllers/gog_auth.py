# -*- coding: utf-8 -*-
"""Google (gog) OAuth controller — runs gog CLI locally on the Odoo pod.

Auth is decoupled from sandbox — users can authenticate before starting a
sandbox. Auth tokens are stored in the Odoo database (gog_auth_token field)
and copied to sandbox pods via the gog-setup init container.
"""
import json
import logging
import os
import subprocess
import tempfile

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_GOG_TIMEOUT = 45
_GOG_SERVICES = "gmail,calendar,drive,contacts,sheets,docs"
_GOG_BIN = "/usr/local/bin/gog"


def _gog_config_dir(task_id):
    """Per-task gog config directory on the Odoo pod."""
    base = os.path.join(tempfile.gettempdir(), ".atlas-gog-config")
    d = os.path.join(base, str(int(task_id)))
    os.makedirs(d, exist_ok=True)
    return d


def _gog_exec(args, config_dir, keyring_pw="", timeout=_GOG_TIMEOUT):
    """Run gog CLI with an explicit argument list — no shell interpretation."""
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = config_dir
    env["GOG_KEYRING_PASSWORD"] = keyring_pw
    _logger.debug("[GogAuth] exec: %s", args)

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def _collect_gog_files(config_dir):
    """Walk the gogcli directory and return {relative_path: content} dict."""
    gogcli_dir = os.path.join(config_dir, "gogcli")
    if not os.path.isdir(gogcli_dir):
        return {}
    collected = {}
    for dirpath, _dirnames, filenames in os.walk(gogcli_dir):
        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, gogcli_dir)
            try:
                with open(abs_path, "r") as f:
                    collected[rel_path] = f.read()
            except Exception:
                _logger.debug("[GogAuth] could not read %s", abs_path)
    return collected


class AtlasGogAuthController(http.Controller):

    @staticmethod
    def _validate_task(task_id):
        task_id = int(task_id or 0)
        if not task_id:
            return None, {"error": "task_id is required"}
        task = request.env["atlas.atlas"].browse(task_id)
        if not task.exists():
            return None, {"error": "Task not found"}
        return task, None

    @staticmethod
    def _extract_client_secret(gog_auth_raw):
        """Extract client_secret JSON from gog_auth field.

        The field may contain:
        - Raw client_secret.json (has "installed" or "web" key)
        - Combined data with "client_secret" key (after a previous auth)
        Returns (client_secret_str, error_dict_or_None).
        """
        if not gog_auth_raw:
            return None, {
                "error": "Task has no gog_auth data. "
                "Please set the Google OAuth client_secret JSON in the task's Google Auth field."
            }
        try:
            data = json.loads(gog_auth_raw)
        except (json.JSONDecodeError, TypeError):
            return None, {"error": "gog_auth field contains invalid JSON"}

        if not isinstance(data, dict):
            return None, {"error": "gog_auth field must be a JSON object"}

        if "installed" in data or "web" in data:
            return gog_auth_raw, None

        if "client_secret" in data:
            cs = data["client_secret"]
            return json.dumps(cs) if isinstance(cs, dict) else str(cs), None

        return None, {
            "error": "gog_auth field does not contain a valid client_secret.json. "
            "Expected JSON with an 'installed' or 'web' key."
        }

    @http.route("/atlas/gog/start_auth", type="json", auth="user")
    def start_auth(self, task_id=0, **kw):
        _logger.info("[GogAuth] start_auth called task_id=%s", task_id)
        task, err = self._validate_task(task_id)
        if err:
            _logger.warning("[GogAuth] start_auth validation failed: %s", err)
            return err

        email = task.email
        if not email:
            _logger.warning("[GogAuth] task %s has no email", task_id)
            return {"error": "Task has no email configured"}

        client_secret, cs_err = self._extract_client_secret(task.gog_auth)
        if cs_err:
            _logger.warning("[GogAuth] client_secret extraction failed: %s", cs_err)
            return cs_err

        config_dir = _gog_config_dir(task.id)
        keyring_pw = task.password or ""

        _logger.info(
            "[GogAuth] start_auth task=%s email=%s config_dir=%s",
            task_id, email, config_dir,
        )

        gogcli_dir = os.path.join(config_dir, "gogcli")
        os.makedirs(gogcli_dir, exist_ok=True)
        cs_path = os.path.join(gogcli_dir, "client_secret.json")
        try:
            with open(cs_path, "w") as f:
                f.write(client_secret)
        except Exception as exc:
            _logger.exception("[GogAuth] failed to write client_secret.json")
            return {"error": "Failed to write credentials: %s" % exc}

        try:
            stdout, stderr, rc = _gog_exec(
                [_GOG_BIN, "auth", "keyring", "file"],
                config_dir, keyring_pw,
            )
            _logger.info(
                "[GogAuth] keyring setup: rc=%s stdout=%s stderr=%s",
                rc, stdout.strip()[:300], stderr.strip()[:300],
            )
            stdout, stderr, rc = _gog_exec(
                [_GOG_BIN, "auth", "credentials", "set", cs_path],
                config_dir, keyring_pw,
            )
            _logger.info(
                "[GogAuth] credentials setup: rc=%s stdout=%s stderr=%s",
                rc, stdout.strip()[:300], stderr.strip()[:300],
            )
        except subprocess.TimeoutExpired:
            return {"error": "Credentials setup timed out"}
        except Exception as exc:
            _logger.exception("[GogAuth] credentials setup failed")
            return {"error": "Failed to set credentials: %s" % exc}

        try:
            stdout, stderr, rc = _gog_exec(
                [
                    _GOG_BIN, "auth", "add", email,
                    "--services", _GOG_SERVICES,
                    "--remote", "--step", "1", "--force-consent",
                    "--redirect-uri", "http://localhost", "--json",
                ],
                config_dir, keyring_pw,
            )
            _logger.info(
                "[GogAuth] step 1: rc=%s stdout=%s stderr=%s",
                rc, stdout.strip()[:500], stderr.strip()[:500],
            )

            if rc != 0:
                _logger.warning(
                    "[GogAuth] step 1 FAILED: rc=%s stderr=%s stdout=%s",
                    rc, stderr.strip(), stdout.strip(),
                )
                return {"error": "gog auth step 1 failed: %s" % (stderr.strip() or stdout.strip())}

            data = None
            try:
                data = json.loads(stdout)
            except (json.JSONDecodeError, TypeError):
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            data = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue

            if not data:
                return {
                    "error": "Could not parse auth URL from gog output",
                    "raw": stdout[:500],
                }

            auth_url = (
                data.get("auth_url")
                or data.get("url")
                or data.get("authorization_url")
            )
            if not auth_url:
                _logger.warning("[GogAuth] No auth_url in gog response: %s", data)
                return {"error": "No auth_url in gog response", "data": data}

            _logger.info("[GogAuth] step 1 SUCCESS auth_url=%s", auth_url[:100])
            return {"auth_url": auth_url, "email": email}

        except subprocess.TimeoutExpired:
            _logger.error("[GogAuth] step 1 timed out after %ss", _GOG_TIMEOUT)
            return {"error": "gog auth step 1 timed out"}
        except Exception as exc:
            _logger.exception("[GogAuth] step 1 unexpected error")
            return {"error": str(exc)}

    @http.route("/atlas/gog/exchange_token", type="json", auth="user")
    def exchange_token(self, task_id=0, redirect_url="", **kw):
        _logger.info(
            "[GogAuth] exchange_token called task_id=%s redirect_url=%s",
            task_id, redirect_url[:100] if redirect_url else "",
        )
        task, err = self._validate_task(task_id)
        if err:
            _logger.warning("[GogAuth] exchange_token validation failed: %s", err)
            return err

        email = task.email
        if not email:
            return {"error": "Task has no email configured"}

        if not redirect_url:
            return {"error": "redirect_url is required (the localhost URL after auth)"}

        config_dir = _gog_config_dir(task.id)
        keyring_pw = task.password or ""

        _logger.info(
            "[GogAuth] exchange_token task=%s email=%s config_dir=%s",
            task_id, email, config_dir,
        )

        try:
            stdout, stderr, rc = _gog_exec(
                [
                    _GOG_BIN, "auth", "add", email,
                    "--services", _GOG_SERVICES,
                    "--remote", "--step", "2", "--force-consent",
                    "--redirect-uri", "http://localhost",
                    "--auth-url", redirect_url,
                ],
                config_dir, keyring_pw,
            )
            _logger.info(
                "[GogAuth] step 2: rc=%s stdout=%s stderr=%s",
                rc, stdout.strip()[:500], stderr.strip()[:500],
            )

            if rc != 0:
                _logger.warning(
                    "[GogAuth] step 2 FAILED: rc=%s stderr=%s stdout=%s",
                    rc, stderr.strip(), stdout.strip(),
                )
                return {"error": "Token exchange failed: %s" % (stderr.strip() or stdout.strip())}

            gog_auth_data = _collect_gog_files(config_dir)

            _logger.info(
                "[GogAuth] step 2 collected %d config files: %s",
                len(gog_auth_data), list(gog_auth_data.keys()),
            )

            if gog_auth_data:
                save_data = {"tokens": gog_auth_data}
                task.sudo().write({"gog_auth_token": json.dumps(save_data)})
                _logger.info(
                    "[GogAuth] step 2 SUCCESS: saved gog_auth_token to task %s (keys: %s)",
                    task.id, list(gog_auth_data.keys()),
                )

            return {"success": True, "email": email}

        except subprocess.TimeoutExpired:
            _logger.error("[GogAuth] step 2 timed out after %ss", _GOG_TIMEOUT)
            return {"error": "Token exchange timed out"}
        except Exception as exc:
            _logger.exception("[GogAuth] step 2 unexpected error")
            return {"error": str(exc)}

    @http.route("/atlas/gog/status", type="json", auth="user")
    def gog_status(self, task_id=0, **kw):
        task, err = self._validate_task(task_id)
        if err:
            return err

        if task.gog_auth_token:
            try:
                data = json.loads(task.gog_auth_token)
                if data:
                    return {
                        "authenticated": True,
                        "email": task.email or "",
                        "files": list(data.keys()) if isinstance(data, dict) else [],
                    }
            except (json.JSONDecodeError, TypeError):
                pass

        config_dir = _gog_config_dir(task.id)
        keyring_pw = task.password or ""

        try:
            stdout, stderr, rc = _gog_exec(
                [_GOG_BIN, "auth", "list"],
                config_dir, keyring_pw,
            )
            email = task.email or ""
            if email and email in stdout:
                return {"authenticated": True, "email": email, "gog_available": True}
            return {"authenticated": False, "email": email, "gog_available": True}
        except Exception:
            return {
                "authenticated": False,
                "email": task.email or "",
                "gog_available": True,
            }
