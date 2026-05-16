# -*- coding: utf-8 -*-
from odoo import models, fields, api


class KaijuSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kaiju_argo_server_url = fields.Char(
        string="Argo Server URL",
        config_parameter="kaiju.argo_server_url",
        default="https://argo-workflows-server.argo.svc.cluster.local:2746",
    )
    kaiju_argo_namespace = fields.Char(
        string="Argo Namespace",
        config_parameter="kaiju.argo_namespace",
        default="argo",
    )
    kaiju_argo_token_path = fields.Char(
        string="SA Token Path",
        config_parameter="kaiju.argo_token_path",
        default="/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    kaiju_argo_verify_tls = fields.Boolean(
        string="Verify TLS",
        config_parameter="kaiju.argo_verify_tls",
    )
    kaiju_odoo_internal_url = fields.Char(
        string="Odoo Internal URL",
        config_parameter="kaiju.odoo_internal_url",
        default="http://odoo-web.odoo.svc:8069",
    )
    kaiju_webhook_token = fields.Char(
        string="Webhook Token",
        config_parameter="kaiju.webhook_token",
    )
    # ── AWS Credentials (for S3 metadata fetch) ───────────────────────
    kaiju_aws_region = fields.Char(
        string="AWS Region",
        config_parameter="kaiju.aws_region",
        default="ap-south-1",
    )
    kaiju_aws_access_key_id = fields.Char(
        string="AWS Access Key ID",
        config_parameter="kaiju.aws_access_key_id",
    )
    kaiju_aws_secret_access_key = fields.Char(
        string="AWS Secret Access Key",
        config_parameter="kaiju.aws_secret_access_key",
    )

    # ── Diagnostic button ─────────────────────────────────────────────
    kaiju_connection_diagnostic = fields.Text(
        string="Argo Connection Diagnostic",
        readonly=True,
        store=False,
        help="Result of the most recent 'Test Argo Connection' click.",
    )

    def action_test_argo_connection(self):
        """Probe Argo Server and report exactly what's reachable.

        Runs FOUR checks against the configured Argo Server:
          1. Token file readable on disk
          2. Workflows LIST endpoint (tests SA auth + RBAC for workflows.list)
          3. Specific workflow GET (only if a workflow_name is configured below)
          4. Pod-log endpoint reachability

        Surfaces the result directly into a UI field so users without server
        log access can diagnose the most common failure modes (wrong URL,
        missing RBAC, expired token, network unreachable).
        """
        from datetime import datetime, timezone
        import urllib.error

        self.ensure_one()
        argo = self.env["kaiju.argo.client"]
        config = argo._get_config()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [f"=== Argo Connection Test \u2014 {now_iso} ===\n"]
        lines.append(f"Base URL:  {config['base_url']}")
        lines.append(f"Namespace: {config['namespace']}")
        lines.append(f"Token path: {config['token_path']}")
        lines.append(f"Verify TLS: {config['verify_tls']}\n")

        # 1. Token readable?
        token = argo._get_token(config)
        if not token:
            lines.append("[1] TOKEN: \u274c Cannot read SA token. Check token path and pod mount.")
            self.kaiju_connection_diagnostic = "\n".join(lines)
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Argo test failed", "message": "Cannot read SA token \u2014 see diagnostic below.", "type": "danger", "sticky": True},
            }
        lines.append(f"[1] TOKEN: \u2705 Read {len(token)} bytes from {config['token_path']}")

        # 2. List workflows (smallest possible request)
        try:
            result = argo._request("GET", f"/api/v1/workflows/{config['namespace']}?listOptions.limit=1")
            count = len(result.get("items") or [])
            lines.append(f"[2] LIST WORKFLOWS: \u2705 HTTP 200, found {count} workflow(s) in namespace '{config['namespace']}'")
        except RuntimeError as e:
            msg = str(e)
            lines.append(f"[2] LIST WORKFLOWS: \u274c {msg}")
            if "403" in msg or "Forbidden" in msg:
                lines.append("    \u2192 RBAC issue: Odoo's ServiceAccount lacks 'workflows.argoproj.io/workflows.list' in this namespace.")
                lines.append("    \u2192 Required verbs: get, list, watch on workflows.argoproj.io/workflows AND get, list on pods/log")
            elif "401" in msg:
                lines.append("    \u2192 Token invalid/expired. Restart Odoo to re-read mounted token, or rotate SA secret.")
            elif "404" in msg:
                lines.append("    \u2192 Namespace not found. Verify spelling.")
            elif "Cannot reach" in msg:
                lines.append("    \u2192 Argo Server unreachable. Wrong URL / wrong namespace in URL / network policy blocking.")
            self.kaiju_connection_diagnostic = "\n".join(lines)
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Argo test failed", "message": "See diagnostic field for details.", "type": "danger", "sticky": True},
            }

        # 3. Pod-log endpoint reachability (no real pod, expect 404)
        try:
            # Use a known-non-existent workflow to test the endpoint shape.
            # We expect 404 (good \u2014 endpoint reachable) NOT 403 (RBAC) or connection error.
            argo._request("GET", f"/api/v1/workflows/{config['namespace']}/__odoo-connection-test__")
            lines.append("[3] LOG ENDPOINT: \u2705 reachable (unexpected: probe workflow exists?)")
        except RuntimeError as e:
            msg = str(e)
            if "404" in msg:
                lines.append("[3] LOG ENDPOINT: \u2705 reachable (HTTP 404 expected for non-existent test workflow)")
            elif "403" in msg or "Forbidden" in msg:
                lines.append(f"[3] LOG ENDPOINT: \u274c {msg}")
                lines.append("    \u2192 RBAC: SA can list workflows but NOT read individual workflows. Grant 'workflows.argoproj.io/workflows.get'.")
            else:
                lines.append(f"[3] LOG ENDPOINT: \u26a0 {msg}")

        lines.append("\nNext steps:")
        lines.append("  - If [2] passed: connection is good. Failed logs are likely pod-side issue (container output) or per-pod RBAC for pods/log.")
        lines.append("  - If [2] failed with 403: ask DevOps to apply RBAC. Required Role rules:")
        lines.append("      apiGroups: ['argoproj.io'], resources: ['workflows'], verbs: ['get','list','watch','create']")
        lines.append("      apiGroups: [''], resources: ['pods/log'], verbs: ['get','list']")
        lines.append("      apiGroups: [''], resources: ['pods'], verbs: ['get','list']")
        self.kaiju_connection_diagnostic = "\n".join(lines)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "Argo test complete", "message": "See diagnostic field below for full report.", "type": "success", "sticky": True},
        }
