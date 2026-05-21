# -*- coding: utf-8 -*-
"""Argo Workflows REST API client for in-cluster communication.

Provides an AbstractModel with methods to submit WorkflowTemplates,
stop, and terminate workflows via Argo Server's HTTP API.
"""

import json
import logging
import urllib.error
import urllib.request

from odoo import models, api

_logger = logging.getLogger(__name__)

PARAM_ARGO_URL = "kaiju.argo_server_url"
PARAM_ARGO_NAMESPACE = "kaiju.argo_namespace"
PARAM_ARGO_TOKEN_PATH = "kaiju.argo_token_path"
PARAM_ARGO_VERIFY_TLS = "kaiju.argo_verify_tls"

DEFAULT_ARGO_URL = "http://argo-workflows-server.argo-workflows.svc.cluster.local:2746"
DEFAULT_NAMESPACE = "argo"
DEFAULT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


class ArgoClient(models.AbstractModel):
    """Argo Server REST API client.

    Uses ir.config_parameter for runtime configuration:
      - kaiju.argo_server_url: Base URL of Argo Server (in-cluster)
      - kaiju.argo_namespace: Argo namespace for workflows
      - kaiju.argo_token_path: Filesystem path to SA bearer token
      - kaiju.argo_verify_tls: "true"/"false" — verify TLS cert
    """

    _name = "kaiju.argo.client"
    _description = "Argo Workflows API Client"

    # ── Configuration helpers ────────────────────────────────────────────────

    def _get_config(self):
        """Read Argo configuration from ir.config_parameter."""
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "base_url": ICP.get_param(PARAM_ARGO_URL, DEFAULT_ARGO_URL).rstrip("/"),
            "namespace": ICP.get_param(PARAM_ARGO_NAMESPACE, DEFAULT_NAMESPACE),
            "token_path": ICP.get_param(PARAM_ARGO_TOKEN_PATH, DEFAULT_TOKEN_PATH),
            "verify_tls": ICP.get_param(PARAM_ARGO_VERIFY_TLS, "false") == "true",
        }

    def _get_token(self, config):
        """Read bearer token from the configured path."""
        try:
            with open(config["token_path"], "r") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError) as e:
            _logger.warning(
                "Cannot read Argo SA token at %s: %s", config["token_path"], e
            )
            return ""

    def _request(self, method, path, body=None):
        """Execute HTTP request against Argo Server.

        Returns parsed JSON response or raises RuntimeError.
        """
        import ssl

        config = self._get_config()
        url = f"{config['base_url']}{path}"
        token = self._get_token(config)

        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        ctx = None
        if not config["verify_tls"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            _logger.error(
                "Argo API error: %s %s → HTTP %d: %s",
                method,
                url,
                e.code,
                body_text[:500],
            )
            raise RuntimeError(
                f"Argo API request failed: HTTP {e.code} — {body_text[:200]}"
            ) from e
        except urllib.error.URLError as e:
            _logger.error(
                "Argo API connection error: %s %s → %s", method, url, e.reason
            )
            raise RuntimeError(
                f"Cannot reach Argo Server at {config['base_url']}: {e.reason}"
            ) from e

    # ── Public API ───────────────────────────────────────────────────────────

    def submit_workflow(self, template_name, parameters, labels=None):
        """Submit a workflow from a WorkflowTemplate.

        Args:
            template_name: Name of the WorkflowTemplate to submit.
            parameters: dict of {param_name: param_value} to pass.
            labels: Optional dict of {key: value} labels for the workflow.

        Returns:
            str: The generated workflow name (metadata.name).
        """
        config = self._get_config()
        namespace = config["namespace"]

        # Argo expects parameters as ["key=value", ...] strings
        param_list = [f"{k}={v}" for k, v in parameters.items() if v is not None]

        body = {
            "resourceKind": "WorkflowTemplate",
            "resourceName": template_name,
            "submitOptions": {
                "parameters": param_list,
            },
        }
        if labels:
            body["submitOptions"]["labels"] = ",".join(
                f"{k}={v}" for k, v in labels.items()
            )

        result = self._request("POST", f"/api/v1/workflows/{namespace}/submit", body)

        workflow_name = result.get("metadata", {}).get("name", "")
        if not workflow_name:
            raise RuntimeError(f"Argo submit returned no workflow name: {result}")

        _logger.info(
            "Submitted workflow %s from template %s (namespace=%s)",
            workflow_name,
            template_name,
            namespace,
        )
        return workflow_name

    def stop_workflow(self, workflow_name, message="Stopped by Odoo"):
        """Gracefully stop a workflow (running nodes finish, no new nodes start).

        Args:
            workflow_name: Argo workflow name.
            message: Stop reason message.
        """
        config = self._get_config()
        namespace = config["namespace"]

        self._request(
            "PUT",
            f"/api/v1/workflows/{namespace}/{workflow_name}/stop",
            {"message": message},
        )
        _logger.info("Stopped workflow %s: %s", workflow_name, message)

    def terminate_workflow(self, workflow_name):
        """Immediately terminate a workflow (kills all pods).

        Args:
            workflow_name: Argo workflow name.
        """
        config = self._get_config()
        namespace = config["namespace"]

        self._request(
            "PUT",
            f"/api/v1/workflows/{namespace}/{workflow_name}/terminate",
            {},
        )
        _logger.info("Terminated workflow %s", workflow_name)
