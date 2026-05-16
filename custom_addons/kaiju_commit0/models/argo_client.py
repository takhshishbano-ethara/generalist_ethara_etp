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
        """Execute HTTP request against an Argo SSE endpoint and return
        the full event-stream body as a string.

        Sets `Accept: text/event-stream` (required by Argo's HTTP1 facade).
        Reads the response line-by-line until EOF so the stream is drained
        properly — SSE responses don't carry Content-Length so a plain
        `resp.read()` can return empty or hang depending on the urllib
        buffering behaviour.
        """
        config = self._get_config()
        url = f"{config['base_url']}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        token = self._get_token(config)

        req = urllib.request.Request(url, method=method)
        # SSE: tell server to send event-stream framing.
        req.add_header("Accept", "text/event-stream")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        ctx = None
        if not config["verify_tls"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _logger.info("Argo SSE endpoint returned 404: %s %s", method, url)
                return ""
            body_text = e.read().decode() if e.fp else ""
            _logger.error(
                "Argo SSE error: %s %s → HTTP %d: %s",
                method, url, e.code, body_text[:500],
            )
            raise RuntimeError(
                f"Argo API request failed: HTTP {e.code} — {body_text[:200]}"
            ) from e
        except urllib.error.URLError as e:
            _logger.error(
                "Argo SSE connection error: %s %s → %s", method, url, e.reason
            )
            raise RuntimeError(
                f"Cannot reach Argo Server at {config['base_url']}: {e.reason}"
            ) from e

        # Read the SSE stream line-by-line. The server holds the connection
        # open and sends `data: {...}\n` frames; when the pod is finished and
        # follow=true, the server closes the stream cleanly (EOF). With
        # follow=false on some Argo versions the stream is empty.
        chunks = []
        try:
            with resp:
                while True:
                    line = resp.readline()
                    if not line:
                        break  # EOF
                    chunks.append(line.decode("utf-8", errors="replace"))
        except (OSError, ssl.SSLError) as e:
            # Stream interrupted — return whatever we got so far.
            _logger.warning("SSE stream interrupted for %s: %s", url, e)
        return "".join(chunks)

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
            # Resolve the effective template name. Argo's own helper
            # `GetTemplateFromNode` (workflow/util/util.go) prefers
            # `node.templateRef.template` when present (i.e. when the
            # DAG task uses `templateRef:` to point at another
            # WorkflowTemplate), falling back to `node.templateName`
            # (inline templates). This is the SAME value Argo's
            # controller uses to compute pod names — see
            # workflow/controller/workflowpod.go:209.
            tmpl_ref = node.get("templateRef") or {}
            effective_template = (
                tmpl_ref.get("template")
                or node.get("templateName")
                or ""
            )
            # Derive a clean human-readable name:
            # 1. Prefer Argo's `displayName` (typically the DAG step name, e.g. "prepare")
            # 2. Strip the workflow prefix from `name` (e.g. "wf.prepare" → "prepare")
            # 3. Fall back to the resolved template, then raw node_id
            display = node.get("displayName") or ""
            if not display:
                name = node.get("name") or ""
                if "." in name:
                    display = name.rsplit(".", 1)[-1]
                elif name:
                    display = name
                else:
                    display = effective_template or node_id
            # Prefer Argo's authoritative `podName` if present (v3.5+ emits
            # this directly on the node). Otherwise compute via PodNameV2.
            pod_name = node.get("podName") or self._generate_pod_name(
                workflow_name=workflow_name,
                node_name=node.get("name", ""),
                template_name=effective_template,
                node_id=node.get("id", node_id),
            )
            pod_nodes.append({
                "id": node.get("id", node_id),
                "name": node.get("name", ""),
                "displayName": display,
                "type": node.get("type", ""),
                "phase": node.get("phase", ""),
                # Expose the RESOLVED template name (templateRef-aware) so
                # downstream consumers (_sync_steps) store the correct value.
                "templateName": effective_template,
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

        Mirrors workflow/util/pod_name.go in argo-workflows v3.5+:

            if workflow_name == node_name:  return workflow_name
            if ".inline" in node_name:      prefix = workflow_name
            else:                            prefix = f"{wf}-{template_name}"
            return f"{prefix}-{fnv1a_32(node_name)}"

        IMPORTANT: `template_name` MUST be the RESOLVED template name
        (i.e., `templateRef.template` for templateRef nodes, or the inline
        `templateName` for inline nodes). Use Argo's `GetTemplateFromNode`
        helper logic upstream (in `list_workflow_nodes`) to compute this.
        Falls back to node_id if required fields are missing.
        """
        if not workflow_name or not node_name:
            return node_id or ""
        if workflow_name == node_name:
            return workflow_name
        if ".inline" in node_name:
            prefix = workflow_name
        elif template_name:
            prefix = f"{workflow_name}-{template_name}"
        else:
            # No template name resolved — best effort, mirrors Argo's
            # own behavior (produces a `wf--hash` double-dash name).
            prefix = workflow_name
        # Truncate prefix to leave room for the hash suffix
        if len(prefix) > cls._MAX_POD_PREFIX_LEN - 1:
            prefix = prefix[: cls._MAX_POD_PREFIX_LEN - 1]
        h = cls._fnv1a_32(node_name)
        return f"{prefix}-{h}"

    # ── Pod log fetching ───────────────────────────────────────────────────

    def get_pod_logs(self, workflow_name, pod_name, container="main", tail_lines=None):
        """Fetch logs for a single pod via Argo's SSE log endpoint.

        Uses the recommended WorkflowLogs endpoint
        `/api/v1/workflows/{ns}/{wf}/log?podName=X` (the dedicated PodLogs
        endpoint is marked DEPRECATED in upstream Argo as of v3.5+). Falls
        back to the deprecated path if the new endpoint fails for any reason.

        Parses SSE stream lines and returns concatenated log content.
        Returns empty string on 404 or if no data.
        Raises RuntimeError on other HTTP errors.
        """
        config = self._get_config()
        namespace = config["namespace"]

        # logOptions.follow=true is REQUIRED for Argo's SSE endpoint to emit
        # `data:` frames consistently. With follow=false, the stream often
        # completes immediately without sending log content. For completed
        # pods, follow=true still terminates quickly because the pod is done.
        base_params = {
            "logOptions.container": container,
            "logOptions.follow": "true",
        }
        if tail_lines is not None:
            base_params["logOptions.tailLines"] = str(tail_lines)

        # Primary: WorkflowLogs endpoint (recommended, non-deprecated).
        primary_path = f"/api/v1/workflows/{namespace}/{workflow_name}/log"
        primary_params = dict(base_params, podName=pod_name)
        _logger.info(
            "Fetching pod logs (WorkflowLogs): namespace=%s workflow=%s pod=%s container=%s",
            namespace, workflow_name, pod_name, container,
        )
        raw = ""
        try:
            raw = self._request_stream("GET", primary_path, params=primary_params, timeout=60)
        except RuntimeError as e:
            _logger.warning(
                "WorkflowLogs endpoint failed for pod=%s: %s. Trying deprecated PodLogs endpoint.",
                pod_name, e,
            )
            try:
                fallback_path = f"/api/v1/workflows/{namespace}/{workflow_name}/{pod_name}/log"
                raw = self._request_stream("GET", fallback_path, params=base_params, timeout=60)
            except RuntimeError as e2:
                _logger.error(
                    "Both log endpoints failed for pod=%s: primary=%s fallback=%s",
                    pod_name, e, e2,
                )
                raise

        if not raw:
            _logger.info(
                "Empty response from Argo log endpoint for pod=%s (workflow=%s)",
                pod_name, workflow_name,
            )
            return ""

        # Parse the response stream. Argo's grpc-gateway can emit logs in TWO
        # different on-the-wire formats depending on version/config:
        #
        #   1. True SSE (RFC standard):  `data: {"result":...}\n\n`
        #      Used by Argo's official HTTP1 client and browser EventSource.
        #
        #   2. NDJSON (grpc-gateway raw): `{"result":...}\n`
        #      What grpc-gateway v1.16.0 actually emits in ForwardResponseStream
        #      when no custom Delimiter is set (Argo's marshaler doesn't set one).
        #
        # We accept BOTH formats. See research:
        #   - https://github.com/argoproj/argo-workflows/blob/main/pkg/apiclient/http1/server-sent-events-client.go
        #   - https://github.com/grpc-ecosystem/grpc-gateway/blob/v1.16.0/runtime/handler.go
        #   - https://github.com/argoproj/argo-workflows/issues/13384 (empty response root cause)
        output_lines = []
        frame_count = 0
        skipped_count = 0
        decoded_format = None  # "sse" or "ndjson" — set on first successful parse
        for line in raw.splitlines():
            line = line.rstrip("\r")  # strip optional CRLF artifact
            if not line:
                continue
            if line.startswith(":"):
                continue  # SSE comment / keepalive
            # Strip SSE `data: ` prefix if present; otherwise treat line as raw JSON
            if line.startswith("data: "):
                payload = line[6:]
            elif line.startswith("data:"):
                payload = line[5:]  # tolerate missing space after colon
            else:
                payload = line
            # Argo wraps every gRPC message as `{"result": <LogEntry>}`
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError as e:
                _logger.debug("Skipping malformed log frame: %r (%s)", payload[:200], e)
                skipped_count += 1
                continue
            frame_count += 1
            # Detect format on first parse for diagnostic logging
            if decoded_format is None:
                decoded_format = "sse" if line.startswith("data:") else "ndjson"
            # Extract content. Argo schema: {"result": {"content": "...", "podName": "..."}}
            result = evt.get("result") or {}
            content = result.get("content")
            # IMPORTANT: don't drop empty-string content — it may be a legitimate
            # blank log line. Only drop None.
            if content is not None:
                output_lines.append(content)
            elif evt.get("error"):
                # grpc-gateway error frame: {"error":{"http_code":N,"message":"..."}}
                err = evt["error"]
                _logger.warning(
                    "Argo log endpoint returned error frame for pod=%s: %s",
                    pod_name, err.get("message") or err,
                )
        _logger.info(
            "Argo log parse: format=%s frames=%d lines=%d skipped=%d raw_bytes=%d pod=%s",
            decoded_format or "unknown", frame_count, len(output_lines),
            skipped_count, len(raw), pod_name,
        )
        return "\n".join(output_lines)
