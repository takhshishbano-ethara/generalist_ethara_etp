# -*- coding: utf-8 -*-
"""Google (gog) OAuth controller — runs gog CLI inside the K8s sandbox pod.

The gog binary lives in the openclaw container image, not on the Odoo host.
All gog commands are executed via ``kubectl exec`` against the running sandbox
pod for the given task.
"""
import json
import logging
import subprocess

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_GOG_TIMEOUT = 45
_GOG_SERVICES = "gmail,calendar,drive,contacts,sheets,docs"
_GOG_BIN = "/usr/local/bin/gog"
_GOG_CONFIG_DIR = "/home/node/.config"

try:
    from kubernetes import client as k8s_client, config as k8s_config

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


def _k8s_namespace():
    """Read the namespace from Odoo system parameters, default 'talos'."""
    try:
        ns = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.k8s_namespace", "talos")
            .strip()
        )
        return ns or "talos"
    except Exception:
        return "talos"


def _find_pod_for_sandbox(sandbox_id):
    """Find the running K8s pod name for a sandbox.

    Uses the same label selector as talos_sandbox.py — looks for pods with
    ``app.kubernetes.io/name=talos-sandbox,task-id={sandbox_id}``.
    Returns (pod_name, namespace) or (None, namespace).
    """
    if not K8S_AVAILABLE:
        return None, "talos"

    try:
        k8s_config.load_incluster_config()
    except Exception:
        _logger.warning("[GogAuth] Could not load in-cluster K8s config")
        return None, "talos"

    namespace = _k8s_namespace()
    label_selector = "app.kubernetes.io/name=talos-sandbox,task-id=%s" % sandbox_id

    try:
        core_v1 = k8s_client.CoreV1Api()
        pods = core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        )
        for pod in pods.items:
            phase = (pod.status.phase or "").lower()
            if phase not in ("failed", "unknown", "succeeded"):
                return pod.metadata.name, namespace
    except Exception as e:
        _logger.warning(
            "[GogAuth] Failed to find K8s pod for sandbox %s: %s", sandbox_id, e
        )

    return None, namespace


def _kubectl_exec(pod_name, namespace, command, timeout=_GOG_TIMEOUT):
    """Run a shell command inside the openclaw container via kubectl exec.

    Returns (stdout, stderr, returncode).
    """
    cmd = [
        "kubectl", "exec",
        "-n", namespace,
        pod_name,
        "-c", "openclaw",
        "--",
        "sh", "-c", command,
    ]
    _logger.debug("[GogAuth] kubectl exec: %s", command[:200])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stdout, result.stderr, result.returncode


def _find_running_sandbox(task):
    """Find a running sandbox for this task. Returns (sandbox, error_dict).

    Prefers the claude sandbox, falls back to any running sandbox.
    """
    sandboxes = task.sandbox_ids.filtered(lambda s: s.docker_status == "running")
    if not sandboxes:
        return None, {
            "error": "No running sandbox found for this task. "
            "Please start a sandbox first, then try Google auth."
        }
    claude = sandboxes.filtered(lambda s: s.model_type == "claude")
    return (claude[0] if claude else sandboxes[0]), None


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

    def _resolve_pod(self, task):
        """Find the running sandbox pod for a task.

        Returns (pod_name, namespace, sandbox, error_dict).
        """
        sandbox, err = _find_running_sandbox(task)
        if err:
            return None, None, None, err

        pod_name, namespace = _find_pod_for_sandbox(sandbox.id)
        if not pod_name:
            return None, None, None, {
                "error": "Could not find a running pod for sandbox %s. "
                "The sandbox may still be starting — please wait and retry." % sandbox.id
            }
        return pod_name, namespace, sandbox, None

    @http.route("/talos/gog/start_auth", type="json", auth="user")
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

        pod_name, namespace, sandbox, pod_err = self._resolve_pod(task)
        if pod_err:
            _logger.warning("[GogAuth] pod resolution failed: %s", pod_err)
            return pod_err

        _logger.info(
            "[GogAuth] start_auth task=%s email=%s pod=%s ns=%s",
            task_id, email, pod_name, namespace,
        )

        keyring_pw = task.password or ""

        escaped_secret = client_secret.replace("'", "'\\''")
        setup_cmd = (
            "mkdir -p {config}/gogcli && "
            "echo '{secret}' > {config}/gogcli/client_secret.json && "
            "export GOG_KEYRING_PASSWORD='{keyring_pw}' && "
            "export XDG_CONFIG_HOME={config} && "
            "{gog} auth keyring file 2>&1 && "
            "{gog} auth credentials set {config}/gogcli/client_secret.json 2>&1"
        ).format(
            config=_GOG_CONFIG_DIR,
            secret=escaped_secret,
            keyring_pw=keyring_pw.replace("'", "'\\''"),
            gog=_GOG_BIN,
        )

        try:
            stdout, stderr, rc = _kubectl_exec(pod_name, namespace, setup_cmd)
            _logger.info(
                "[GogAuth] credentials setup: rc=%s stdout=%s stderr=%s",
                rc, stdout.strip()[:300], stderr.strip()[:300],
            )
        except subprocess.TimeoutExpired:
            return {"error": "Credentials setup timed out"}
        except Exception as exc:
            _logger.exception("[GogAuth] credentials setup failed")
            return {"error": "Failed to set credentials: %s" % exc}

        step1_cmd = (
            "export GOG_KEYRING_PASSWORD='{keyring_pw}' && "
            "export XDG_CONFIG_HOME={config} && "
            "{gog} auth add {email} "
            "--services {services} "
            "--remote --step 1 --force-consent "
            "--redirect-uri http://localhost --json 2>&1"
        ).format(
            keyring_pw=keyring_pw.replace("'", "'\\''"),
            config=_GOG_CONFIG_DIR,
            gog=_GOG_BIN,
            email=email,
            services=_GOG_SERVICES,
        )

        try:
            stdout, stderr, rc = _kubectl_exec(pod_name, namespace, step1_cmd)
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

    @http.route("/talos/gog/exchange_token", type="json", auth="user")
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

        pod_name, namespace, sandbox, pod_err = self._resolve_pod(task)
        if pod_err:
            _logger.warning("[GogAuth] pod resolution failed: %s", pod_err)
            return pod_err

        keyring_pw = task.password or ""

        _logger.info(
            "[GogAuth] exchange_token pod=%s ns=%s email=%s",
            pod_name, namespace, email,
        )

        escaped_url = redirect_url.replace("'", "'\\''")
        step2_cmd = (
            "export GOG_KEYRING_PASSWORD='{keyring_pw}' && "
            "export XDG_CONFIG_HOME={config} && "
            "{gog} auth add {email} "
            "--services {services} "
            "--remote --step 2 --force-consent "
            "--redirect-uri http://localhost "
            "--auth-url '{auth_url}' 2>&1"
        ).format(
            keyring_pw=keyring_pw.replace("'", "'\\''"),
            config=_GOG_CONFIG_DIR,
            gog=_GOG_BIN,
            email=email,
            services=_GOG_SERVICES,
            auth_url=escaped_url,
        )

        try:
            stdout, stderr, rc = _kubectl_exec(pod_name, namespace, step2_cmd)
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

            collect_cmd = (
                "cd {config}/gogcli 2>/dev/null && "
                "find . -type f | while read f; do "
                '  rel=$(echo "$f" | sed "s|^\\./||"); '
                '  echo "---FILE:$rel"; '
                '  cat "$f"; '
                "done"
            ).format(config=_GOG_CONFIG_DIR)

            file_stdout, _, file_rc = _kubectl_exec(pod_name, namespace, collect_cmd)

            gog_auth_data = {}
            if file_rc == 0 and file_stdout.strip():
                current_file = None
                current_content = []
                for line in file_stdout.splitlines():
                    if line.startswith("---FILE:"):
                        if current_file is not None:
                            gog_auth_data[current_file] = "\n".join(current_content)
                        current_file = line[len("---FILE:"):]
                        current_content = []
                    else:
                        current_content.append(line)
                if current_file is not None:
                    gog_auth_data[current_file] = "\n".join(current_content)

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

    @http.route("/talos/gog/status", type="json", auth="user")
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

        pod_name, namespace, sandbox, pod_err = self._resolve_pod(task)
        if pod_err:
            return {
                "authenticated": False,
                "email": task.email or "",
                "gog_available": False,
                "detail": "No running sandbox to check gog status",
            }

        keyring_pw = task.password or ""
        list_cmd = (
            "export GOG_KEYRING_PASSWORD='{keyring_pw}' && "
            "export XDG_CONFIG_HOME={config} && "
            "{gog} auth list 2>&1"
        ).format(
            keyring_pw=keyring_pw.replace("'", "'\\''"),
            config=_GOG_CONFIG_DIR,
            gog=_GOG_BIN,
        )

        try:
            stdout, stderr, rc = _kubectl_exec(pod_name, namespace, list_cmd)
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
