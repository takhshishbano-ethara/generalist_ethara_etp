# -*- coding: utf-8 -*-
"""Argo Workflows REST API client for in-cluster communication.

Provides an AbstractModel with methods to submit WorkflowTemplates,
query status, stop, and terminate workflows via Argo Server's HTTP API.
"""

import json
import logging
import ssl
import urllib.parse
import urllib.error
import urllib.request

from odoo import models, api

_logger = logging.getLogger(__name__)

PARAM_ARGO_URL = "kaiju.argo_server_url"
PARAM_ARGO_NAMESPACE = "kaiju.argo_namespace"
PARAM_ARGO_TOKEN_PATH = "kaiju.argo_token_path"
PARAM_ARGO_VERIFY_TLS = "kaiju.argo_verify_tls"

DEFAULT_ARGO_URL = "https://argo-workflows-server.argo.svc.cluster.local:2746"
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

    def get_workflow_status(self, workflow_name):
        """Get workflow status from Argo.

        Returns:
            dict with keys: phase, progress, message, nodes, started_at, finished_at
        """
        config = self._get_config()
        namespace = config["namespace"]

        result = self._request("GET", f"/api/v1/workflows/{namespace}/{workflow_name}")
        status = result.get("status", {})

        return {
            "phase": status.get("phase", ""),
            "progress": status.get("progress", ""),
            "message": status.get("message", ""),
            "started_at": status.get("startedAt", ""),
            "finished_at": status.get("finishedAt", ""),
            "nodes": status.get("nodes", {}),
        }

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

    # ── Streaming helper ────────────────────────────────────────────────────

    def _request_stream(self, method, path, params=None, timeout=60):
        """Execute HTTP request and return raw response body as string.

        Used for SSE / streaming endpoints (e.g. pod logs) where the
        response is NOT JSON.
        """
        config = self._get_config()
        url = f"{config['base_url']}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        token = self._get_token(config)

        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        ctx = None
        if not config["verify_tls"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
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

    # ── Workflow node listing ──────────────────────────────────────────────

    def list_workflow_nodes(self, workflow_name):
        """Return list of Pod-typed node dicts from a workflow's status.nodes.

        Each dict: {id, name, displayName, type, phase, templateName,
                     message, startedAt, finishedAt}.
        Returns [] if workflow not found (404) or has no nodes.
        """
        config = self._get_config()
        namespace = config["namespace"]

        try:
            result = self._request(
                "GET", f"/api/v1/workflows/{namespace}/{workflow_name}"
            )
        except RuntimeError:
            return []

        nodes = result.get("status", {}).get("nodes", {})
        if not nodes:
            return []

        pod_nodes = []
        for node_id, node in nodes.items():
            if node.get("type") != "Pod":
                continue
            # Derive a clean human-readable name:
            # 1. Prefer Argo's `displayName` (typically the DAG step name, e.g. "prepare")
            # 2. Strip the workflow prefix from `name` (e.g. "wf.prepare" → "prepare")
            # 3. Fall back to `templateName`, then raw node_id
            display = node.get("displayName") or ""
            if not display:
                name = node.get("name") or ""
                if "." in name:
                    display = name.rsplit(".", 1)[-1]
                elif name:
                    display = name
                else:
                    display = node.get("templateName") or node_id
            pod_name = self._generate_pod_name(
                workflow_name=workflow_name,
                node_name=node.get("name", ""),
                template_name=node.get("templateName", ""),
                node_id=node.get("id", node_id),
            )
            pod_nodes.append({
                "id": node.get("id", node_id),
                "name": node.get("name", ""),
                "displayName": display,
                "type": node.get("type", ""),
                "phase": node.get("phase", ""),
                "templateName": node.get("templateName", ""),
                "message": node.get("message", ""),
                "startedAt": node.get("startedAt", ""),
                "finishedAt": node.get("finishedAt", ""),
                "podName": pod_name,
            })
        return pod_nodes

    # ── Pod name generation (Argo v2 naming) ───────────────────────────────

    # Argo v3.5+ defaults to PodNameV2: pod names are NOT the raw node_id.
    # The algorithm (from argo-workflows/workflow/util/pod_name.go):
    #   if workflow_name == node_name:  return workflow_name
    #   else: return f"{workflow_name}-{template_name}-{fnv1a_32(node_name)}"
    # The fnv1a-32 hash is of the node NAME (not node_id), as an unsigned 32-bit int.
    # See: https://github.com/argoproj/argo-workflows/blob/main/workflow/util/pod_name.go

    _MAX_K8S_NAME_LEN = 253
    _K8S_HASH_LEN = 10
    _MAX_POD_PREFIX_LEN = _MAX_K8S_NAME_LEN - _K8S_HASH_LEN  # 243

    @staticmethod
    def _fnv1a_32(data: str) -> int:
        """FNV-1a 32-bit hash matching Go's hash/fnv New32a()."""
        h = 0x811C9DC5
        prime = 0x01000193
        mask = 0xFFFFFFFF
        for b in data.encode("utf-8"):
            h ^= b
            h = (h * prime) & mask
        return h

    @classmethod
    def _generate_pod_name(cls, workflow_name, node_name, template_name, node_id):
        """Replicate Argo's GeneratePodName (PodNameV2) algorithm.

        Returns the actual Kubernetes pod name for a given workflow node,
        matching the Go implementation in argo-workflows/workflow/util/pod_name.go.
        Falls back to node_id if required fields are missing.
        """
        if not workflow_name or not node_name:
            return node_id or ""
        if workflow_name == node_name:
            return workflow_name
        prefix = workflow_name
        if template_name:
            prefix = f"{prefix}-{template_name}"
        # Truncate prefix to leave room for the hash suffix
        if len(prefix) > cls._MAX_POD_PREFIX_LEN - 1:
            prefix = prefix[: cls._MAX_POD_PREFIX_LEN - 1]
        h = cls._fnv1a_32(node_name)
        return f"{prefix}-{h}"

    # ── Pod log fetching ───────────────────────────────────────────────────

    def get_pod_logs(self, workflow_name, pod_name, container="main", tail_lines=None):
        """Fetch logs for a single pod via Argo SSE log endpoint.

        Parses SSE stream lines and returns concatenated log content.
        Returns empty string on 404 or if no data.
        Raises RuntimeError on other HTTP errors.
        """
        config = self._get_config()
        namespace = config["namespace"]

        params = {
            "podName": pod_name,
            "logOptions.container": container,
            "logOptions.follow": "false",
        }
        if tail_lines is not None:
            params["logOptions.tailLines"] = str(tail_lines)

        path = f"/api/v1/workflows/{namespace}/{workflow_name}/log"
        raw = self._request_stream("GET", path, params=params, timeout=60)
        if not raw:
            return ""

        output_lines = []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
                result = evt.get("result", {})
                content = result.get("content")
                if content:
                    output_lines.append(content)
            except json.JSONDecodeError:
                continue
        return "\n".join(output_lines)
