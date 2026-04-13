# -*- coding: utf-8 -*-
import json
import logging
import os
import shutil
import subprocess

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_GOG_TIMEOUT = 30
_GOG_SERVICES = "gmail,calendar,drive,contacts,sheets,docs"


def _find_gog_binary():
    return shutil.which("gog")


def _gog_env(keyring_password=None):
    env = os.environ.copy()
    if keyring_password:
        env["GOG_KEYRING_PASSWORD"] = keyring_password
    return env


def _gog_config_dir(task_id):
    data_dir = os.environ.get("HOME", "/tmp")
    d = os.path.join(data_dir, ".talos-gog-config", str(task_id))
    os.makedirs(d, exist_ok=True)
    return d


class TalosGogAuthController(http.Controller):

    @staticmethod
    def _validate_task(task_id):
        task_id = int(task_id or 0)
        if not task_id:
            return None, {"error": "task_id is required"}
        task = request.env["talos.talos"].browse(task_id)
        if not task.exists():
            return None, {"error": "Task not found"}
        return task, None

    @staticmethod
    def _extract_client_secret(gog_auth_raw):
        """Extract client_secret JSON from gog_auth field.

        The field may contain:
        - Raw client_secret.json (has "installed" or "web" key)
        - Combined data with "client_secret" key (after a previous auth)
        - Auth token files from gog (dict of filename->content)
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

        # Case 1: Raw client_secret.json (has "installed" or "web" top-level key)
        if "installed" in data or "web" in data:
            return gog_auth_raw, None

        # Case 2: Combined format — {"client_secret": {...}, "tokens": {...}}
        if "client_secret" in data:
            cs = data["client_secret"]
            return json.dumps(cs) if isinstance(cs, dict) else str(cs), None

        return None, {
            "error": "gog_auth field does not contain a valid client_secret.json. "
            "Expected JSON with an 'installed' or 'web' key."
        }

    @http.route("/talos/gog/start_auth", type="json", auth="user")
    def start_auth(self, task_id=0, **kw):
        _logger.info("[GogAuth] start_auth called task_id=%s", task_id)
        task, err = self._validate_task(task_id)
        if err:
            _logger.warning("[GogAuth] start_auth validation failed: %s", err)
            return err

        gog_bin = _find_gog_binary()
        if not gog_bin:
            _logger.error("[GogAuth] gog binary not found in PATH")
            return {"error": "gog binary not found on the host system"}

        email = task.email
        if not email:
            _logger.warning("[GogAuth] task %s has no email", task_id)
            return {"error": "Task has no email configured"}

        _logger.info("[GogAuth] start_auth task=%s email=%s gog_bin=%s", task_id, email, gog_bin)
        keyring_pw = task.password or ""
        client_secret, cs_err = self._extract_client_secret(task.gog_auth)
        if cs_err:
            _logger.warning("[GogAuth] client_secret extraction failed: %s", cs_err)
            return cs_err

        config_dir = _gog_config_dir(task.id)
        _logger.info("[GogAuth] config_dir=%s", config_dir)
        env = _gog_env(keyring_pw)
        env["XDG_CONFIG_HOME"] = config_dir

        secret_path = os.path.join(config_dir, "client_secret.json")
        with open(secret_path, "w") as f:
            f.write(client_secret)
        _logger.info("[GogAuth] wrote client_secret.json to %s", secret_path)

        try:
            kr_result = subprocess.run(
                [gog_bin, "auth", "keyring", "file"],
                env=env,
                capture_output=True,
                text=True,
                timeout=_GOG_TIMEOUT,
                check=False,
            )
            _logger.info("[GogAuth] keyring file: rc=%s stdout=%s stderr=%s",
                         kr_result.returncode, kr_result.stdout.strip(), kr_result.stderr.strip())

            cred_result = subprocess.run(
                [gog_bin, "auth", "credentials", "set", secret_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=_GOG_TIMEOUT,
                check=False,
            )
            _logger.info("[GogAuth] credentials set: rc=%s stdout=%s stderr=%s",
                         cred_result.returncode, cred_result.stdout.strip(), cred_result.stderr.strip())
        except Exception as exc:
            _logger.exception("Failed to set gog credentials")
            return {"error": "Failed to set credentials: %s" % exc}

        try:
            cmd = [
                gog_bin, "auth", "add", email,
                "--services", _GOG_SERVICES,
                "--remote",
                "--step", "1",
                "--force-consent",
                "--redirect-uri", "http://localhost",
                "--json",
            ]
            _logger.info("[GogAuth] step 1 cmd: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=_GOG_TIMEOUT,
                check=False,
            )
            _logger.info("[GogAuth] step 1: rc=%s stdout=%s stderr=%s",
                         result.returncode, result.stdout.strip()[:500], result.stderr.strip()[:500])

            if result.returncode != 0:
                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                _logger.warning(
                    "[GogAuth] step 1 FAILED: rc=%s stderr=%s stdout=%s",
                    result.returncode, stderr, stdout,
                )
                return {"error": "gog auth step 1 failed: %s" % (stderr or stdout)}

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            data = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue
                else:
                    return {"error": "Could not parse auth URL from gog output", "raw": result.stdout}

            auth_url = data.get("auth_url") or data.get("url") or data.get("authorization_url")
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

    @http.route("/talos/gog/exchange_token", type="json", auth="user")
    def exchange_token(self, task_id=0, redirect_url="", **kw):
        _logger.info("[GogAuth] exchange_token called task_id=%s redirect_url=%s", task_id, redirect_url[:100] if redirect_url else "")
        task, err = self._validate_task(task_id)
        if err:
            _logger.warning("[GogAuth] exchange_token validation failed: %s", err)
            return err

        gog_bin = _find_gog_binary()
        if not gog_bin:
            _logger.error("[GogAuth] gog binary not found in PATH")
            return {"error": "gog binary not found on the host system"}

        email = task.email
        if not email:
            return {"error": "Task has no email configured"}

        if not redirect_url:
            return {"error": "redirect_url is required (the localhost URL after auth)"}

        keyring_pw = task.password or ""

        original_client_secret = None
        cs_str, _ = self._extract_client_secret(task.gog_auth)
        if cs_str:
            try:
                original_client_secret = json.loads(cs_str)
            except (json.JSONDecodeError, TypeError):
                pass

        config_dir = _gog_config_dir(task.id)
        _logger.info("[GogAuth] exchange_token config_dir=%s email=%s", config_dir, email)
        env = _gog_env(keyring_pw)
        env["XDG_CONFIG_HOME"] = config_dir

        try:
            cmd = [
                gog_bin, "auth", "add", email,
                "--services", _GOG_SERVICES,
                "--remote",
                "--step", "2",
                "--force-consent",
                "--redirect-uri", "http://localhost",
                "--auth-url", redirect_url,
            ]
            _logger.info("[GogAuth] step 2 cmd: %s", " ".join(cmd[:8]) + " --auth-url <url>")
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=_GOG_TIMEOUT,
                check=False,
            )
            _logger.info("[GogAuth] step 2: rc=%s stdout=%s stderr=%s",
                         result.returncode, result.stdout.strip()[:500], result.stderr.strip()[:500])

            if result.returncode != 0:
                stderr = result.stderr.strip()
                stdout = result.stdout.strip()
                _logger.warning(
                    "[GogAuth] step 2 FAILED: rc=%s stderr=%s stdout=%s",
                    result.returncode, stderr, stdout,
                )
                return {"error": "Token exchange failed: %s" % (stderr or stdout)}

            gog_config_path = os.path.join(config_dir, "gogcli")
            gog_auth_data = {}
            if os.path.isdir(gog_config_path):
                for root, dirs, files in os.walk(gog_config_path):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        rel = os.path.relpath(fpath, gog_config_path)
                        try:
                            with open(fpath, "r") as f:
                                gog_auth_data[rel] = f.read()
                        except Exception:
                            pass
            _logger.info("[GogAuth] step 2 collected %d config files: %s",
                         len(gog_auth_data), list(gog_auth_data.keys()))

            if gog_auth_data:
                save_data = {"tokens": gog_auth_data}
                if original_client_secret:
                    save_data["client_secret"] = original_client_secret
                task.sudo().write({"gog_auth": json.dumps(save_data)})
                _logger.info("[GogAuth] step 2 SUCCESS: saved gog_auth to task %s (keys: %s)",
                             task.id, list(save_data.keys()))

            return {"success": True, "email": email}

        except subprocess.TimeoutExpired:
            _logger.error("[GogAuth] step 2 timed out after %ss", _GOG_TIMEOUT)
            return {"error": "Token exchange timed out"}
        except Exception as exc:
            _logger.exception("[GogAuth] step 2 unexpected error")
            return {"error": str(exc)}

    @http.route("/talos/gog/status", type="json", auth="user")
    def gog_status(self, task_id=0, **kw):
        task, err = self._validate_task(task_id)
        if err:
            return err

        if task.gog_auth:
            try:
                data = json.loads(task.gog_auth)
                if data:
                    return {
                        "authenticated": True,
                        "email": task.email or "",
                        "files": list(data.keys()) if isinstance(data, dict) else [],
                    }
            except (json.JSONDecodeError, TypeError):
                pass

        gog_bin = _find_gog_binary()
        if not gog_bin:
            return {"authenticated": False, "email": task.email or "", "gog_available": False}

        keyring_pw = task.password or ""

        config_dir = _gog_config_dir(task.id)
        env = _gog_env(keyring_pw)
        env["XDG_CONFIG_HOME"] = config_dir

        try:
            result = subprocess.run(
                [gog_bin, "auth", "list"],
                env=env,
                capture_output=True,
                text=True,
                timeout=_GOG_TIMEOUT,
                check=False,
            )
            email = task.email or ""
            if email and email in result.stdout:
                return {"authenticated": True, "email": email, "gog_available": True}
            return {"authenticated": False, "email": email, "gog_available": True}
        except Exception:
            return {"authenticated": False, "email": task.email or "", "gog_available": True}
