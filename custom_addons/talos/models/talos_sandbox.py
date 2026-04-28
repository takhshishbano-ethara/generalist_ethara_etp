import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

from .talos import (
    _DEFAULT_LITELLM_CONFIG,
    _HEALTH_POLL_INTERVAL,
    _HEALTH_WAIT_TIMEOUT,
    _compose_cmd,
    _docker_available,
    _load_dotenv,
    _module_sandbox_dir,
    _wrap_messages_with_turn_feedback,
    _wrap_trajectory_message,
    generate_task_description_sync,
)

_logger = logging.getLogger(__name__)

_SANDBOX_POOL_WORKERS = int(os.getenv("SANDBOX_POOL_WORKERS", "3"))
_SANDBOX_POOL = ThreadPoolExecutor(
    max_workers=_SANDBOX_POOL_WORKERS, thread_name_prefix="talos-sandbox"
)
_SANDBOX_STARTING = set()
_SANDBOX_LOCK = threading.Lock()

MODEL_TYPES = [
    ("claude", "Claude Opus 4.7"),
    ("glm", "Kimi K2.5"),
    ("1pa", "1PA"),
    ("1pb", "1PB"),
    ("1pc", "1PC"),
    ("1pd", "1PD"),
]

MODEL_DEFAULTS = {
    "claude": "litellm/claude-opus-4.7",
    "glm": "litellm/kimi-k2.5",
    "1pa": "litellm/quiet_sand",
    "1pb": "litellm/quiet_sand",
    "1pc": "litellm/quiet_sand",
    "1pd": "litellm/quiet_sand",
}

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432

TRAJECTORY_FIELD_MAP = {
    "claude": "claude_trajectory",
    "glm": "glm_trajectory",
    "1pa": "onePA_trajectory",
    "1pb": "onePB_trajectory",
    "1pc": "onePC_trajectory",
    "1pd": "onePD_trajectory",
}


def _mark_task_description_status(db_name, task_id, field_name, status, entry_index=-1):
    """Update the task_description_status on a trajectory entry."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["talos.talos"].browse(task_id)
            if not task.exists():
                return
            raw = task[field_name] or ""
            if not raw.strip():
                return
            data = json.loads(raw)
            if isinstance(data, list) and data:
                idx = entry_index if 0 <= entry_index < len(data) else -1
                if data[idx].get("task_description_status") == "aborted":
                    return
                data[idx]["task_description_status"] = status
                task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})
    except Exception:
        _logger.exception(
            "Failed to mark task_description_status=%s for %s task %s",
            status,
            field_name,
            task_id,
        )


def _inject_task_description_bg(
    db_name, task_id, field_name, seed_prompt, messages, entry_index=-1
):
    """Background: generate task description via GLM and inject into saved trajectory."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            desc, usage = generate_task_description_sync(env, seed_prompt, messages)
            if usage.get("input_tokens", 0) > 0 or usage.get("output_tokens", 0) > 0:
                task_rec = env["talos.talos"].browse(task_id)
                if task_rec.exists():
                    task_rec.write(
                        {
                            "taskdesc_input_tokens": (
                                task_rec.taskdesc_input_tokens or 0
                            )
                            + usage.get("input_tokens", 0),
                            "taskdesc_output_tokens": (
                                task_rec.taskdesc_output_tokens or 0
                            )
                            + usage.get("output_tokens", 0),
                        }
                    )
            if not desc:
                _mark_task_description_status(
                    db_name, task_id, field_name, "done", entry_index
                )
                return
            task = env["talos.talos"].browse(task_id)
            if not task.exists():
                return
            raw = task[field_name] or ""
            if not raw.strip():
                return
            data = json.loads(raw)
            if isinstance(data, list) and data:
                idx = entry_index if 0 <= entry_index < len(data) else -1
                if data[idx].get("task_description_status") == "aborted":
                    return
                mi = data[idx].setdefault("trajectory", {}).setdefault("meta_info", {})
                mi["task_description"] = desc
                mi["task_completion_status"] = "success"
                data[idx]["task_description_status"] = "done"
            elif isinstance(data, dict):
                mi = data.setdefault("meta_info", {})
                mi["task_description"] = desc
                mi["task_completion_status"] = "success"
            task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})
            _logger.info(
                "Injected task_description (%d chars) into %s for task %s",
                len(desc),
                field_name,
                task_id,
            )
    except Exception:
        _logger.exception(
            "Failed to inject task_description into %s for task %s",
            field_name,
            task_id,
        )
        _mark_task_description_status(db_name, task_id, field_name, "done", entry_index)


def _run_sandbox_start_background(db_name, sandbox_id, mode, notify_partner_id):
    """Background worker: start sandbox (docker compose or K8s), then notify via bus.bus."""
    final_status = "error"
    error_msg = ""
    model_type = ""
    try:
        # Phase 1: snapshot what we need (short cursor)
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["talos.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                _logger.error(
                    "Background sandbox start: sandbox %s does not exist", sandbox_id
                )
                return
            model_type = sandbox.model_type or ""

        # Phase 2: long-running work (separate cursor per _bg method)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["talos.sandbox"].browse(sandbox_id)
                if mode == "k8s":
                    sandbox._start_k8s_bg()
                else:
                    sandbox._start_local_bg()
        except Exception as e:
            _logger.exception(
                "Background sandbox start failed for sandbox %s: %s",
                sandbox_id,
                e,
            )
            error_msg = str(e)[:1000]

        # Phase 3: read final status + notify (fresh cursor, retry on conflict)
        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["talos.sandbox"].browse(sandbox_id)
                    if not sandbox.exists():
                        return

                    if error_msg and sandbox.docker_status != "running":
                        sandbox.write(
                            {
                                "docker_status": "error",
                                "docker_error": error_msg,
                            }
                        )

                    final_status = sandbox.docker_status
                    error_msg = sandbox.docker_error or ""

                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if not partner:
                        partner = sandbox.employee_id.user_id.partner_id
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "talos/sandbox_ready",
                            {
                                "sandbox_id": sandbox_id,
                                "docker_status": final_status,
                                "error": error_msg,
                                "model_type": model_type,
                            },
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    _logger.warning(
                        "Serialization conflict in Phase 3 for sandbox %s, retry %d",
                        sandbox_id,
                        attempt + 1,
                    )
                    time.sleep(1 + attempt)
                    continue
                raise
    except Exception:
        _logger.exception("Background sandbox start crashed (sandbox=%s)", sandbox_id)
    finally:
        with _SANDBOX_LOCK:
            _SANDBOX_STARTING.discard(sandbox_id)


class TalosSandbox(models.Model):
    _name = "talos.sandbox"
    _description = "Talos Sandbox"
    _order = "model_type"

    talos_id = fields.Many2one(
        "talos.talos", required=True, ondelete="cascade", index=True
    )
    employee_id = fields.Many2one(
        related="talos_id.employee_id", store=True, readonly=True
    )
    model_type = fields.Selection(MODEL_TYPES, required=True, readonly=True)

    # Docker lifecycle fields (moved from talos.talos)
    docker_compose_project = fields.Char(readonly=True, copy=False)
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("error", "Error"),
        ],
        default="stopped",
        readonly=True,
    )
    docker_port = fields.Integer(readonly=True)
    docker_litellm_port = fields.Integer(readonly=True)
    docker_gateway_token = fields.Char(readonly=True, copy=False)
    docker_dashboard_url = fields.Char(compute="_compute_dashboard_url")
    docker_ws_url = fields.Char(compute="_compute_docker_ws_url")
    docker_error = fields.Text(readonly=True)
    docker_workdir = fields.Char(readonly=True, copy=False)

    # Session tracking
    session_status = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
        ],
        default="not_started",
    )

    # Auto-hint loop state
    auto_hint_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("evaluating", "Evaluating"),
            ("sending_hint", "Sending Hint"),
            ("streaming", "Streaming"),
            ("max_retries", "Max Retries Reached"),
            ("error", "Error"),
        ],
        default="idle",
        help="Current state of the automated hint loop.",
    )
    auto_hint_iteration = fields.Integer(
        string="Auto Hint Current Iteration",
        default=0,
        help="Current iteration count of the in-flight auto-hint loop (0 = idle).",
    )
    auto_hint_group_id = fields.Char(
        string="Auto Hint Group ID",
        help="UUID of the currently active auto-hint loop.",
    )

    # Turns
    turn_ids = fields.One2many("talos.turn", "sandbox_id", string="Turns")

    _sql_constraints = [
        (
            "unique_task_model",
            "UNIQUE(talos_id, model_type)",
            "Each task can only have one sandbox per model type.",
        ),
    ]

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
            .strip()
        )

    @api.depends(
        "docker_port", "docker_gateway_token", "docker_status", "docker_compose_project"
    )
    def _compute_dashboard_url(self):
        for rec in self:
            if rec.docker_status != "running" or not rec.docker_gateway_token:
                rec.docker_dashboard_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    rec.env["ir.config_parameter"]
                    .sudo()
                    .get_param("talos.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    rec.docker_dashboard_url = "https://%s/sandbox/%s/#token=%s" % (
                        ws_host,
                        rec.id,
                        rec.docker_gateway_token,
                    )
                else:
                    svc_name = "talos-sandbox-%s" % rec.id
                    rec.docker_dashboard_url = (
                        "http://%s.talos.svc.cluster.local:18789/#token=%s"
                        % (svc_name, rec.docker_gateway_token)
                    )
            else:
                if rec.docker_port:
                    rec.docker_dashboard_url = "http://localhost:%d/#token=%s" % (
                        rec.docker_port,
                        rec.docker_gateway_token,
                    )
                else:
                    rec.docker_dashboard_url = False

    @api.depends("docker_port", "docker_status")
    def _compute_docker_ws_url(self):
        for rec in self:
            if rec.docker_status != "running" or not rec.docker_port:
                rec.docker_ws_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    self.env["ir.config_parameter"]
                    .sudo()
                    .get_param("talos.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    rec.docker_ws_url = "wss://%s/sandbox/%s/" % (ws_host, rec.id)
                else:
                    rec.docker_ws_url = False
            else:
                rec.docker_ws_url = "ws://localhost:%d" % rec.docker_port

    def _get_gateway_ws_url(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            svc_name = "talos-sandbox-%s" % self.id
            return "ws://%s.talos.svc.cluster.local:18789" % svc_name
        else:
            if not self.docker_port:
                return False
            return "ws://localhost:%d" % self.docker_port

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _allocate_ports(self):
        self.ensure_one()
        offset = self.id % 5000
        return (
            GATEWAY_PORT_BASE + offset,
            LITELLM_PORT_BASE + offset,
            DB_PORT_BASE + offset,
        )

    # ------------------------------------------------------------------
    # JSONL extraction from OpenClaw container
    # ------------------------------------------------------------------

    def _read_session_jsonl(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            return self._read_jsonl_k8s()
        return self._read_jsonl_local()

    def _read_jsonl_local(self):
        self.ensure_one()
        workdir = self.docker_workdir
        if not workdir or not os.path.isdir(workdir):
            return []

        persona = self.talos_id.persona_id
        persona_name = persona.name if persona else "marcus"
        sessions_dir = os.path.join(
            workdir, "data", persona_name, "agents", "main", "sessions"
        )
        if not os.path.isdir(sessions_dir):
            _logger.warning(
                "Sessions dir not found: %s (sandbox=%s)", sessions_dir, self.id
            )
            return []

        jsonl_files = sorted(
            [f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")],
            key=lambda f: os.path.getmtime(os.path.join(sessions_dir, f)),
        )
        if not jsonl_files:
            _logger.warning("No JSONL files in %s (sandbox=%s)", sessions_dir, self.id)
            return []

        _logger.info(
            "Reading %d JSONL file(s) from %s (sandbox=%s)",
            len(jsonl_files),
            sessions_dir,
            self.id,
        )

        entries = []
        for fname in jsonl_files:
            jsonl_path = os.path.join(sessions_dir, fname)
            with open(jsonl_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def _read_jsonl_k8s(self):
        self.ensure_one()
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            k8s_config.load_incluster_config()
        except Exception:
            _logger.warning(
                "K8s not available for JSONL extraction (sandbox=%s)", self.id
            )
            return []

        pod_name = None
        namespace = "default"
        try:
            ns_param = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("talos.k8s_namespace", "talos")
                .strip()
            )
            if ns_param:
                namespace = ns_param
        except Exception:
            pass

        task_id = self.id
        label_selector = "app.kubernetes.io/name=talos-sandbox,task-id=%s" % task_id
        try:
            core_v1 = k8s_client.CoreV1Api()
            pods = core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )
            for pod in pods.items:
                phase = (pod.status.phase or "").lower()
                if phase not in ("failed", "unknown"):
                    pod_name = pod.metadata.name
                    break
        except Exception as e:
            _logger.warning("Failed to find K8s pod for sandbox %s: %s", self.id, e)
            return []

        if not pod_name:
            _logger.warning("No running pod found for sandbox %s", self.id)
            return []

        try:
            result = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    namespace,
                    pod_name,
                    "-c",
                    "openclaw",
                    "--",
                    "sh",
                    "-c",
                    "cat /home/node/.openclaw/agents/main/sessions/*.jsonl 2>/dev/null",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                _logger.warning(
                    "kubectl exec returned no data for sandbox %s: %s",
                    self.id,
                    result.stderr[:200],
                )
                return []

            entries = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return entries
        except Exception as e:
            _logger.warning("kubectl exec failed for sandbox %s: %s", self.id, e)
            return []

    # Fields that are internal to OpenClaw and must NOT appear in the
    # delivered trajectory JSON.
    _INTERNAL_MSG_FIELDS = {
        "sender",
        "api",
        "provider",
        "model",
        "usage",
    }
    _INTERNAL_BLOCK_FIELDS = {"api", "provider", "model", "usage"}

    @staticmethod
    def _sanitize_jsonl_message(msg):
        """Strip internal OpenClaw metadata from a JSONL message before export."""
        msg = dict(msg)

        content_before = msg.get("content", [])
        thinking_before = [
            b
            for b in (content_before if isinstance(content_before, list) else [])
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        if thinking_before:
            _logger.info(
                "[THINKING-DEBUG] _sanitize_jsonl_message BEFORE: role=%s thinking_blocks=%d "
                "first_thinking_len=%d has_signature=%s",
                msg.get("role", "?"),
                len(thinking_before),
                len(thinking_before[0].get("thinking", "")),
                bool(thinking_before[0].get("thinkingSignature")),
            )

        for key in TalosSandbox._INTERNAL_MSG_FIELDS:
            msg.pop(key, None)

        content = msg.get("content")
        if isinstance(content, list):
            cleaned = []
            for block in content:
                if isinstance(block, dict):
                    block = dict(block)
                    for key in TalosSandbox._INTERNAL_BLOCK_FIELDS:
                        block.pop(key, None)
                    tcid = block.get("toolCallId", "")
                    if isinstance(tcid, str) and "|" in tcid:
                        block["toolCallId"] = tcid.split("|", 1)[0]
                    tc_id = block.get("id", "")
                    if (
                        block.get("type") == "tool_use"
                        and isinstance(tc_id, str)
                        and "|" in tc_id
                    ):
                        block["id"] = tc_id.split("|", 1)[0]
                cleaned.append(block)
            msg["content"] = cleaned

        thinking_after = [
            b
            for b in (
                msg.get("content", []) if isinstance(msg.get("content"), list) else []
            )
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        if thinking_before and not thinking_after:
            _logger.error(
                "[THINKING-DEBUG] _sanitize_jsonl_message LOST thinking blocks! "
                "before=%d after=%d role=%s",
                len(thinking_before),
                len(thinking_after),
                msg.get("role", "?"),
            )

        return msg

    def _build_trajectory_from_jsonl(self, entries):
        self.ensure_one()
        task = self.talos_id
        model_name = ""
        default = MODEL_DEFAULTS.get(self.model_type)
        if default:
            model_name = default.replace("litellm/", "")

        meta_info = {
            "task_type": task.task_type or "",
            "task_description": task.task_id or "",
            "task_completion_status": "success",
            "system_prompt": task.system_prompt or "",
            "platform": "macOS",
            "conv_id": str(uuid.uuid4()),
        }

        messages = []
        last_kept_id = None
        seen_user_msg = False

        for entry in entries:
            entry_type = entry.get("type", "")
            if entry_type != "message":
                continue

            msg = entry.get("message", {})
            role = msg.get("role", "")
            if not role:
                continue

            # Skip session startup messages (system init before first user message)
            if role == "user":
                seen_user_msg = True
            elif role == "system" and not seen_user_msg:
                continue

            msg = self._sanitize_jsonl_message(msg)

            entry_id = entry.get("id", "")
            parent_id = last_kept_id if last_kept_id else entry.get("parentId", "")

            delivery_msg = {
                "type": "message",
                "id": entry_id,
                "parentId": parent_id or "",
                "timestamp": entry.get("timestamp", ""),
                "message": msg,
            }
            messages.append(delivery_msg)
            last_kept_id = entry_id

        # Apply hint/feedback wrappers from Odoo turn data
        all_turns = self.turn_ids.sorted("turn_number")
        if all_turns:
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = [_wrap_trajectory_message(m) for m in messages]

        return {"meta_info": meta_info, "messages": messages}

    @staticmethod
    def _extract_tokens_from_jsonl(entries):
        total_in = 0
        total_out = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            usage = entry.get("usage") or {}
            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = usage or msg.get("usage") or {}
            total_in += int(
                usage.get("input_tokens", 0) or usage.get("inputTokens", 0) or 0
            )
            total_out += int(
                usage.get("output_tokens", 0) or usage.get("outputTokens", 0) or 0
            )
        return total_in, total_out

    def _query_litellm_spend(self):
        self.ensure_one()
        import urllib.error
        import urllib.parse
        import urllib.request

        mode = self._deployment_mode()
        litellm_key = ""

        if mode == "k8s":
            ws_host = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("talos.ws_router_host", "")
                .strip()
            )
            if not ws_host:
                _logger.warning(
                    "No ws_router_host configured, cannot query LiteLLM spend (sandbox=%s)",
                    self.id,
                )
                return 0, 0
            base_url = "https://%s/litellm/%s" % (ws_host, self.id)
            dotenv = _load_dotenv()
            litellm_key = dotenv.get("LITELLM_MASTER_KEY", "").strip()
            if not litellm_key:
                litellm_key = (
                    "sk-talos-%s" % self.docker_gateway_token[:16]
                    if self.docker_gateway_token
                    else ""
                )
        else:
            litellm_port = self.docker_litellm_port
            if not litellm_port:
                return 0, 0
            base_url = "http://localhost:%d" % litellm_port
            dotenv = _load_dotenv()
            litellm_key = dotenv.get("LITELLM_MASTER_KEY", "").strip()

        if not litellm_key:
            _logger.warning(
                "No LITELLM_MASTER_KEY, cannot query LiteLLM spend (sandbox=%s)",
                self.id,
            )
            return 0, 0

        try:
            start_date = (
                self.create_date.strftime("%Y-%m-%d") if self.create_date else ""
            )
            if not start_date:
                return 0, 0

            from datetime import datetime as dt
            from datetime import timedelta

            end_date = (dt.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            params = urllib.parse.urlencode(
                {
                    "start_date": start_date,
                    "end_date": end_date,
                }
            )
            url = "%s/spend/logs?%s" % (base_url, params)

            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": "Bearer %s" % litellm_key,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            logs = data if isinstance(data, list) else data.get("data", [])

            total_in = 0
            total_out = 0
            for entry in logs:
                total_in += int(entry.get("prompt_tokens", 0) or 0)
                total_out += int(entry.get("completion_tokens", 0) or 0)

            _logger.info(
                "LiteLLM spend query returned %d logs (in=%d, out=%d) for sandbox %s",
                len(logs),
                total_in,
                total_out,
                self.id,
            )
            return total_in, total_out

        except urllib.error.HTTPError as e:
            _logger.warning(
                "LiteLLM spend API error for sandbox %s: %s %s",
                self.id,
                e.code,
                e.reason,
            )
            return 0, 0
        except Exception as e:
            _logger.warning(
                "LiteLLM spend query failed for sandbox %s: %s",
                self.id,
                e,
            )
            return 0, 0

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/talos/chat/export_session?sandbox_id=%d" % self.id,
            "target": "self",
        }

    def build_trajectory_json(self):
        self.ensure_one()
        task = self.talos_id
        model_name = ""
        all_turns = self.turn_ids.sorted("turn_number")
        for t in reversed(all_turns):
            if t.model_name:
                model_name = t.model_name
                break
        if not model_name:
            default = MODEL_DEFAULTS.get(self.model_type)
            if default:
                model_name = default.replace("litellm/", "")

        meta_info = {
            "task_type": task.task_type or "",
            "task_description": task.task_id or "",
            "task_completion_status": "success",
            "system_prompt": task.system_prompt or "",
            "platform": "macOS",
            "persona": task.persona_id.name if task.persona_id else "",
            "model": model_name,
            "difficulty": task.difficulty or "",
            "conv_id": str(uuid.uuid4()),
        }

        messages = self._trajectory_from_ws()
        if messages:
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_ws path (%d messages)",
                self.id,
                len(messages),
            )
            thinking_count = sum(
                1
                for m in messages
                for b in (m.get("message", m) if isinstance(m, dict) else {}).get(
                    "content", []
                )
                or []
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s ws messages thinking_blocks=%d",
                self.id,
                thinking_count,
            )
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = self._trajectory_from_events()
            if messages:
                _logger.info(
                    "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_events path (%d messages)",
                    self.id,
                    len(messages),
                )
        if not messages:
            messages = self._trajectory_from_turns()
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_turns path (%d messages)",
                self.id,
                len(messages),
            )

        return {"meta_info": meta_info, "messages": messages}

    def _trajectory_from_ws(self):
        self.ensure_one()
        best_messages = []
        best_count = 0
        for t in self.turn_ids.sorted("turn_number", reverse=True):
            if t.trajectory_messages:
                try:
                    ws_messages = json.loads(t.trajectory_messages)
                    if isinstance(ws_messages, list) and ws_messages:
                        thinking_in_turn = sum(
                            1
                            for m in ws_messages
                            for b in (
                                (
                                    m.get("message", m) if isinstance(m, dict) else {}
                                ).get("content", [])
                                or []
                            )
                            if isinstance(b, dict) and b.get("type") == "thinking"
                        )
                        _logger.info(
                            "[THINKING-DEBUG] _trajectory_from_ws: turn=%s turn_number=%s "
                            "messages=%d thinking_blocks=%d",
                            t.id,
                            t.turn_number,
                            len(ws_messages),
                            thinking_in_turn,
                        )
                        if len(ws_messages) > best_count:
                            best_messages = ws_messages
                            best_count = len(ws_messages)
                except (json.JSONDecodeError, TypeError):
                    continue
        if not best_messages:
            _logger.info(
                "[THINKING-DEBUG] _trajectory_from_ws: NO trajectory_messages found "
                "in %d turns (sandbox=%s). Turns with trajectory_messages: %s",
                len(self.turn_ids),
                self.id,
                [t.id for t in self.turn_ids if t.trajectory_messages],
            )
        return best_messages

    def _trajectory_from_events(self):
        self.ensure_one()
        turns = self.turn_ids.sorted("turn_number")
        messages = []
        msg_counter = 0
        parent_id = None

        for t in turns:
            run_id = t.run_id or ""
            user_text = (t.prompt or t.hints or "").strip()
            if t.hints:
                is_accepted = 1
                hints = t.hints.strip()
            else:
                is_accepted = 0
                hints = None

            def _next_id():
                nonlocal msg_counter
                msg_counter += 1
                return "%s:%d" % (run_id, msg_counter) if run_id else ""

            if user_text:
                user_id = _next_id()
                messages.append(
                    {
                        "type": "message",
                        "id": user_id,
                        "parentId": parent_id,
                        "timestamp": t.prompt_timestamp
                        or (t.create_date.isoformat() if t.create_date else ""),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": user_text}],
                        },
                    }
                )
                parent_id = user_id

            if t.raw_events:
                try:
                    events = json.loads(t.raw_events)
                    if isinstance(events, list) and events:
                        pre_count = len(messages)
                        messages, msg_counter, parent_id = (
                            self.talos_id._build_trajectory_from_events(
                                events,
                                messages,
                                msg_counter,
                                parent_id,
                                t.model_name or "",
                            )
                        )
                        for idx in range(pre_count, len(messages)):
                            messages[idx] = _wrap_trajectory_message(
                                messages[idx], is_accepted, hints
                            )
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

            if t.tool_calls:
                try:
                    calls = json.loads(t.tool_calls)
                    if isinstance(calls, list):
                        for tc in calls:
                            tcid = tc.get("toolCallId", "")
                            call_id = tcid or _next_id()
                            call_msg = {
                                "type": "message",
                                "id": call_id,
                                "parentId": parent_id,
                                "timestamp": t.response_timestamp
                                or (t.write_date.isoformat() if t.write_date else ""),
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "toolCall",
                                            "id": tcid or call_id,
                                            "name": tc.get("name", "unknown"),
                                            "arguments": tc.get("args", {}),
                                        }
                                    ],
                                },
                            }
                            messages.append(
                                _wrap_trajectory_message(call_msg, is_accepted, hints)
                            )
                            parent_id = call_id

                            result_id = ("%s:result" % tcid) if tcid else _next_id()
                            result_text = tc.get("result")
                            if isinstance(result_text, dict):
                                result_text = json.dumps(result_text)
                            elif result_text is None:
                                result_text = ""
                            else:
                                result_text = str(result_text)
                            result_msg = {
                                "type": "message",
                                "id": result_id,
                                "parentId": parent_id,
                                "timestamp": t.response_timestamp
                                or (t.write_date.isoformat() if t.write_date else ""),
                                "message": {
                                    "role": "toolResult",
                                    "toolCallId": tcid or call_id,
                                    "toolName": tc.get("name", "unknown"),
                                    "isError": tc.get("isError", False),
                                    "content": [{"type": "text", "text": result_text}],
                                },
                            }
                            messages.append(
                                _wrap_trajectory_message(result_msg, is_accepted, hints)
                            )
                            parent_id = result_id
                except (json.JSONDecodeError, TypeError):
                    pass

            if t.response:
                asst_id = _next_id()
                asst_msg = {
                    "type": "message",
                    "id": asst_id,
                    "parentId": parent_id,
                    "timestamp": t.response_timestamp
                    or (t.write_date.isoformat() if t.write_date else ""),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": t.response}],
                        "model": t.model_name or "",
                    },
                }
                messages.append(_wrap_trajectory_message(asst_msg, is_accepted, hints))
                parent_id = asst_id

        return messages if messages else []

    def _trajectory_from_turns(self):
        self.ensure_one()
        turns = self.turn_ids.sorted("turn_number")
        messages = []
        msg_counter = 0
        parent_id = None

        for t in turns:
            run_id = t.run_id or ""
            user_text = (t.prompt or t.hints or "").strip()
            if t.hints:
                is_accepted = 1
                hints = t.hints.strip()
            else:
                is_accepted = 0
                hints = None

            def _next_id():
                nonlocal msg_counter
                msg_counter += 1
                return "%s:%d" % (run_id, msg_counter) if run_id else ""

            if user_text:
                user_id = _next_id()
                messages.append(
                    {
                        "type": "message",
                        "id": user_id,
                        "parentId": parent_id,
                        "timestamp": t.prompt_timestamp
                        or (t.create_date.isoformat() if t.create_date else ""),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": user_text}],
                        },
                    }
                )
                parent_id = user_id

            if t.response:
                asst_id = _next_id()
                asst_msg = {
                    "type": "message",
                    "id": asst_id,
                    "parentId": parent_id,
                    "timestamp": t.response_timestamp
                    or (t.write_date.isoformat() if t.write_date else ""),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": t.response}],
                        "model": t.model_name or "",
                    },
                }
                messages.append(_wrap_trajectory_message(asst_msg, is_accepted, hints))
                parent_id = asst_id

        return messages

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_start_sandbox(self):
        """Start sandbox asynchronously — returns immediately, work runs in background."""
        self.ensure_one()

        if not self.talos_id:
            raise UserError(
                "Sandbox is not linked to a task (sandbox_id=%s)." % self.id
            )
        if not self.talos_id.persona_id:
            raise UserError(
                "No persona selected on task '%s'. "
                "Please select a persona and save before starting."
                % (self.talos_id.display_name or self.talos_id.id)
            )
        if self.docker_status in ("starting", "running"):
            raise UserError("Sandbox is already %s." % self.docker_status)

        mode = self._deployment_mode()

        # Pre-validate docker availability (Local mode only)
        if mode != "k8s":
            if not _docker_available():
                raise UserError(
                    "Docker is not available on this server. "
                    "Please ensure the Docker daemon is running."
                )
            if not _compose_cmd():
                raise UserError("docker compose (or docker-compose) not found.")

        # Dedup: prevent duplicate concurrent starts
        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                raise UserError("Sandbox start is already in progress.")
            _SANDBOX_STARTING.add(self.id)

        # Generate gateway token + allocate ports immediately
        gateway_token = secrets.token_hex(32)
        write_vals = {
            "docker_status": "starting",
            "docker_error": False,
            "docker_gateway_token": gateway_token,
            # Reset auto-hint state from previous sessions
            "auto_hint_status": "idle",
            "auto_hint_iteration": 0,
            "auto_hint_group_id": False,
        }
        if mode != "k8s":
            gateway_port, litellm_port, db_port = self._allocate_ports()
            write_vals["docker_port"] = gateway_port
            write_vals["docker_litellm_port"] = litellm_port
        self.write(write_vals)

        # Capture context for background thread
        sandbox_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        # Schedule background work AFTER this transaction commits
        @self.env.cr.postcommit.add
        def _queue_sandbox_start():
            _SANDBOX_POOL.submit(
                _run_sandbox_start_background,
                db_name,
                sandbox_id,
                mode,
                notify_partner_id,
            )

        _logger.info(
            "[SANDBOX] action_start_sandbox | sandbox=%s | model=%s | mode=%s | "
            "queued to background pool",
            self.id,
            self.model_type,
            mode,
        )

    def action_stop_sandbox(self):
        self.ensure_one()

        self._export_trajectory_to_task()

        mode = self._deployment_mode()
        if mode == "k8s":
            self._stop_k8s()
        else:
            self._stop_local()

    @staticmethod
    def _count_thinking_blocks(trajectory):
        count = 0
        samples = []
        for msg_envelope in trajectory.get("messages", []):
            inner = msg_envelope
            if isinstance(msg_envelope, dict) and "message" in msg_envelope:
                inner = msg_envelope["message"]
            if not isinstance(inner, dict):
                continue
            content = inner.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    count += 1
                    samples.append(
                        {
                            "thinking_len": len(block.get("thinking", "")),
                            "has_signature": bool(block.get("thinkingSignature")),
                        }
                    )
        return count, samples

    def _export_trajectory_to_task(self):
        self.ensure_one()

        trajectory = None

        jsonl_entries = self._read_session_jsonl()
        if jsonl_entries:
            jsonl_thinking = 0
            for entry in jsonl_entries:
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    for block in msg.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            jsonl_thinking += 1
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s JSONL entries=%d thinking_blocks_in_raw_jsonl=%d",
                self.id,
                len(jsonl_entries),
                jsonl_thinking,
            )
            _logger.info(
                "[JSONL-RAW] sandbox=%s entries=%d\n%s",
                self.id,
                len(jsonl_entries),
                json.dumps(jsonl_entries, indent=2, ensure_ascii=False)[:50000],
            )
            trajectory = self._build_trajectory_from_jsonl(jsonl_entries)
            traj_thinking, traj_samples = self._count_thinking_blocks(trajectory)
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s AFTER _build_trajectory_from_jsonl: "
                "thinking_blocks=%d samples=%s",
                self.id,
                traj_thinking,
                traj_samples[:3],
            )
            _logger.info(
                "Built trajectory from JSONL (%d entries, %d messages, sandbox=%s)",
                len(jsonl_entries),
                len(trajectory.get("messages", [])),
                self.id,
            )
        elif self.turn_ids:
            trajectory = self.build_trajectory_json()
            traj_thinking, traj_samples = self._count_thinking_blocks(trajectory)
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s AFTER build_trajectory_json (turns fallback): "
                "thinking_blocks=%d samples=%s",
                self.id,
                traj_thinking,
                traj_samples[:3],
            )
            _logger.info(
                "Built trajectory from turns fallback (%d messages, sandbox=%s)",
                len(trajectory.get("messages", [])),
                self.id,
            )

        if trajectory:
            field_name = TRAJECTORY_FIELD_MAP.get(self.model_type)
            if field_name and self.talos_id:
                session_entry = {
                    "session_id": secrets.token_hex(8),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "trajectory": trajectory,
                }

                existing_raw = self.talos_id[field_name] or ""
                entries = []
                if existing_raw.strip():
                    try:
                        parsed = json.loads(existing_raw)
                        if isinstance(parsed, list):
                            entries = parsed
                        else:
                            entries = [
                                {
                                    "session_id": "legacy",
                                    "timestamp": "",
                                    "trajectory": parsed,
                                }
                            ]
                    except (json.JSONDecodeError, TypeError):
                        pass

                entries.append(session_entry)
                new_value = json.dumps(entries, indent=2, ensure_ascii=False)

                self.talos_id.write({field_name: new_value})
                _logger.info(
                    "Appended trajectory session %s (%d total entries) to %s for task %s",
                    session_entry["session_id"],
                    len(entries),
                    field_name,
                    self.talos_id.id,
                )

        # Extract token usage: LiteLLM spend API (primary), JSONL (fallback)
        if self.talos_id:
            total_in, total_out = self._query_litellm_spend()
            source = "litellm"
            if total_in == 0 and total_out == 0:
                token_entries = jsonl_entries if jsonl_entries else []
                if token_entries:
                    total_in, total_out = self._extract_tokens_from_jsonl(token_entries)
                    source = "jsonl"
            if total_in > 0 or total_out > 0:
                token_field_map = {
                    "claude": ("claude_input_tokens", "claude_output_tokens"),
                    "glm": ("glm_input_tokens", "glm_output_tokens"),
                    "1pa": ("onePA_input_tokens", "onePA_output_tokens"),
                    "1pb": ("onePB_input_tokens", "onePB_output_tokens"),
                    "1pc": ("onePC_input_tokens", "onePC_output_tokens"),
                    "1pd": ("onePD_input_tokens", "onePD_output_tokens"),
                }
                fields_pair = token_field_map.get(self.model_type)
                if fields_pair:
                    self.talos_id.write(
                        {
                            fields_pair[0]: total_in,
                            fields_pair[1]: total_out,
                        }
                    )
                    _logger.info(
                        "Saved token usage from %s (in=%d, out=%d) to %s/%s for task %s",
                        source,
                        total_in,
                        total_out,
                        fields_pair[0],
                        fields_pair[1],
                        self.talos_id.id,
                    )

        if self.turn_ids:
            # Aggregate bedrock QC tokens to task level before deleting turns
            if self.talos_id:
                bedrock_in = sum(t.bedrock_input_tokens or 0 for t in self.turn_ids)
                bedrock_out = sum(t.bedrock_output_tokens or 0 for t in self.turn_ids)
                if bedrock_in > 0 or bedrock_out > 0:
                    self.talos_id.write(
                        {
                            "bedrock_input_tokens": (
                                self.talos_id.bedrock_input_tokens or 0
                            )
                            + bedrock_in,
                            "bedrock_output_tokens": (
                                self.talos_id.bedrock_output_tokens or 0
                            )
                            + bedrock_out,
                        }
                    )
                    _logger.info(
                        "Aggregated bedrock QC tokens (in=%d, out=%d) to task %s",
                        bedrock_in,
                        bedrock_out,
                        self.talos_id.id,
                    )

                turn_token_map = {
                    "claude": (
                        "claude_input_tokens",
                        "claude_output_tokens",
                        "claude_input_tokens",
                        "claude_output_tokens",
                    ),
                    "glm": (
                        "glm_input_tokens",
                        "glm_output_tokens",
                        "glm_input_tokens",
                        "glm_output_tokens",
                    ),
                }
                turn_fields = turn_token_map.get(self.model_type)
                if turn_fields:
                    turn_in_field, turn_out_field, task_in_field, task_out_field = (
                        turn_fields
                    )
                    t_in = sum(getattr(t, turn_in_field, 0) or 0 for t in self.turn_ids)
                    t_out = sum(
                        getattr(t, turn_out_field, 0) or 0 for t in self.turn_ids
                    )
                    if t_in > 0 or t_out > 0:
                        existing_in = getattr(self.talos_id, task_in_field, 0) or 0
                        existing_out = getattr(self.talos_id, task_out_field, 0) or 0
                        self.talos_id.write(
                            {
                                task_in_field: existing_in + t_in,
                                task_out_field: existing_out + t_out,
                            }
                        )
                        _logger.info(
                            "Aggregated %s turn tokens (in=%d, out=%d) to task %s",
                            self.model_type,
                            t_in,
                            t_out,
                            self.talos_id.id,
                        )

            turn_count = len(self.turn_ids)
            self.turn_ids.unlink()
            _logger.info(
                "Cleared %d turns for sandbox %s (session isolation)",
                turn_count,
                self.id,
            )

    def _start_k8s(self):
        if self.docker_status == "running":
            raise UserError("Sandbox is already running.")

        gateway_token = secrets.token_hex(32)
        self.write(
            {
                "docker_status": "starting",
                "docker_gateway_token": gateway_token,
                "docker_error": False,
            }
        )

        try:
            self.env["talos.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "talos-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_status": "starting",
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (persona=%s, model=%s)",
                svc_name,
                self.id,
                self.talos_id.persona_id.name,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write({"docker_status": "error", "docker_error": str(e)[:1000]})

    def _stop_k8s(self):
        if self.docker_status == "stopped":
            return

        try:
            self.env["talos.sandbox.k8s"].destroy_sandbox(self)
            _logger.info("Destroyed K8s sandbox for sandbox %s", self.id)
        except Exception as e:
            _logger.warning("K8s sandbox destroy failed for sandbox %s: %s", self.id, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_error": False,
            }
        )

    def _start_local(self):
        if self.docker_status == "running" and self.docker_compose_project:
            raise UserError("Docker stack is already running for this sandbox.")

        if not _docker_available():
            raise UserError(
                "Docker is not available on this server. "
                "Please ensure the Docker daemon is running."
            )

        compose_bin = _compose_cmd()
        if not compose_bin:
            raise UserError("docker compose (or docker-compose) not found.")

        persona = self.talos_id.persona_id
        if not persona:
            raise UserError(
                "No persona selected for the parent task (task_id=%s, sandbox_id=%s, talos_id=%s)."
                % (self.talos_id.id, self.id, self.talos_id)
            )

        gateway_token = secrets.token_hex(32)
        project_name = "talos-%d-%s" % (self.talos_id.id, self.model_type)
        gateway_port, litellm_port, db_port = self._allocate_ports()

        try:
            workdir = self._prepare_workdir(
                persona, gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        self.write(
            {
                "docker_compose_project": project_name,
                "docker_status": "starting",
                "docker_port": gateway_port,
                "docker_litellm_port": litellm_port,
                "docker_gateway_token": gateway_token,
                "docker_workdir": workdir,
                "docker_error": False,
            }
        )
        self.env.cr.commit()

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed (exit %d): %s"
                        % (result.returncode, error_msg[:1000]),
                    }
                )
                return

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s persona=%s model=%s",
                    project_name,
                    self.id,
                    persona.name,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )
                _logger.error(
                    "Gateway health-check failed for project %s (sandbox %s)",
                    project_name,
                    self.id,
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write({"docker_status": "error", "docker_error": str(e)[:500]})

    def _start_local_bg(self):
        """Start local Docker sandbox — called from background thread."""
        compose_bin = _compose_cmd()
        persona = self.talos_id.persona_id
        gateway_token = self.docker_gateway_token
        gateway_port = self.docker_port
        litellm_port = self.docker_litellm_port
        db_port = DB_PORT_BASE + (self.id % 5000)
        project_name = "talos-%d-%s" % (self.talos_id.id, self.model_type)

        try:
            workdir = self._prepare_workdir(
                persona, gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed: %s" % error_msg[:1000],
                    }
                )
                return

            self.write(
                {
                    "docker_compose_project": project_name,
                    "docker_workdir": workdir,
                }
            )

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s persona=%s model=%s",
                    project_name,
                    self.id,
                    persona.name,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:500],
                }
            )

    def _start_k8s_bg(self):
        """Start K8s sandbox — called from background thread."""
        try:
            self.env["talos.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "talos-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (persona=%s, model=%s)",
                svc_name,
                self.id,
                self.talos_id.persona_id.name,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:1000],
                }
            )
            return

        # Poll for K8s readiness (up to 5 minutes)
        k8s_model = self.env["talos.sandbox.k8s"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                status = k8s_model.get_sandbox_status(self)
                if status == "running":
                    self.write({"docker_status": "running"})
                    _logger.info(
                        "K8s sandbox %s is now running",
                        self.id,
                    )
                    return
                if status == "error":
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "K8s deployment failed",
                        }
                    )
                    return
            except Exception as e:
                _logger.debug("K8s readiness poll error: %s", e)
            time.sleep(5)

        # Timeout — leave as starting, cron will continue checking
        _logger.warning(
            "K8s sandbox %s did not become ready within 300s, "
            "cron will continue reconciliation",
            self.id,
        )

    def _stop_local(self):
        if self.docker_status == "stopped":
            return

        compose_bin = _compose_cmd()
        project_name = self.docker_compose_project
        workdir = self.docker_workdir

        if compose_bin and project_name and workdir and os.path.isdir(workdir):
            try:
                cmd = compose_bin + ["-p", project_name]
                cmd += ["-f", "docker-compose.yml"]
                override = os.path.join(workdir, "docker-compose.override.yml")
                if os.path.isfile(override):
                    cmd += ["-f", "docker-compose.override.yml"]
                cmd += ["down", "--volumes", "--remove-orphans"]

                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    cwd=workdir,
                )
                _logger.info(
                    "Stopped sandbox (project=%s) sandbox=%s",
                    project_name,
                    self.id,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to stop compose project %s: %s", project_name, e
                )
        elif compose_bin and project_name:
            try:
                subprocess.run(
                    compose_bin
                    + [
                        "-p",
                        project_name,
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except Exception as e:
                _logger.warning("Force stop failed: %s", e)

        if workdir and os.path.isdir(workdir):
            try:
                shutil.rmtree(workdir)
            except Exception as e:
                _logger.warning("Could not clean workdir %s: %s", workdir, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_workdir": False,
                "docker_error": False,
            }
        )

    # ------------------------------------------------------------------
    # Helper methods for local lifecycle
    # ------------------------------------------------------------------

    def _prepare_workdir(
        self, persona, gateway_token, gateway_port, litellm_port, db_port
    ):
        env = _load_dotenv()
        source_dir = _module_sandbox_dir()
        if not source_dir or not os.path.isdir(source_dir):
            raise UserError(
                "Bundled sandbox_docker directory not found in talos module."
            )

        workdir = os.path.join(
            tempfile.gettempdir(),
            "talos-sandbox",
            "talos-%d-%s" % (self.talos_id.id, self.model_type),
        )
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        os.makedirs(workdir)

        for filename in ("Dockerfile", "litellm-patch-entrypoint.sh"):
            src = os.path.join(source_dir, filename)
            dst = os.path.join(workdir, filename)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        if persona.docker_compose_yaml:
            with open(os.path.join(workdir, "docker-compose.yml"), "w") as f:
                f.write(persona.docker_compose_yaml)
        else:
            src = os.path.join(source_dir, "docker-compose.yml")
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(workdir, "docker-compose.yml"))

        persona_dir = os.path.join(workdir, "personas", persona.name)
        os.makedirs(persona_dir)
        for fname, content in [
            ("SOUL.md", persona.soul_md),
            ("MEMORY.md", persona.memory_md),
            ("AGENTS.md", persona.agents_md),
        ]:
            if content:
                with open(os.path.join(persona_dir, fname), "w") as f:
                    f.write(content)

        data_dir = os.path.join(workdir, "data", persona.name)
        os.makedirs(data_dir, exist_ok=True)
        ws_dir = os.path.join(data_dir, "workspace")
        os.makedirs(os.path.join(ws_dir, "memory"), exist_ok=True)
        os.makedirs(os.path.join(ws_dir, "skills"), exist_ok=True)

        for fname, content in [
            ("SOUL.md", persona.soul_md),
            ("MEMORY.md", persona.memory_md),
            ("AGENTS.md", persona.agents_md),
        ]:
            if content:
                with open(os.path.join(ws_dir, fname), "w") as f:
                    f.write(content)

        aws_bearer = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        aws_region = env.get("AWS_REGION", "ap-south-1").strip()
        bedrock_arn = env.get("BEDROCK_MODEL_ARN", "").strip()
        litellm_key = env.get("LITELLM_MASTER_KEY", "").strip()
        if not litellm_key:
            litellm_key = "sk-talos-%s" % secrets.token_hex(8)

        origins = [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
            "http://0.0.0.0:18789",
            "http://localhost:8069",
            "http://127.0.0.1:8069",
        ]
        if gateway_port != 18789:
            origins.append("http://localhost:%d" % gateway_port)
            origins.append("http://127.0.0.1:%d" % gateway_port)

        config = {
            "gateway": {
                "bind": "lan",
                "auth": {"mode": "token", "token": gateway_token},
                "trustedProxies": [
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "10.0.0.0/8",
                ],
                "controlUi": {
                    "allowedOrigins": origins,
                    "dangerouslyDisableDeviceAuth": True,
                },
            },
            "browser": {
                "enabled": True,
                "headless": True,
                "noSandbox": True,
                "defaultProfile": "openclaw",
                "executablePath": "/usr/bin/chromium",
            },
            "models": {"providers": {}},
        }

        providers = config["models"]["providers"]

        if aws_bearer and bedrock_arn:
            providers["talos-bedrock"] = {
                "baseUrl": "https://bedrock-runtime.%s.amazonaws.com" % aws_region,
                "apiKey": aws_bearer,
                "auth": "api-key",
                "api": "bedrock-converse-stream",
                "models": [
                    {
                        "id": bedrock_arn,
                        "name": "claude-inference",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 200000,
                        "maxTokens": 128000,
                    }
                ],
            }
        providers["litellm"] = {
            "baseUrl": "http://litellm:4000/v1",
            "apiKey": litellm_key,
            "auth": "api-key",
            "api": "openai-completions",
            "models": [
                {
                    "id": "claude-opus-4.7",
                    "name": "claude-opus-4.7",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 200000,
                    "maxTokens": 128000,
                },
                {
                    "id": "kimi-k2.5",
                    "name": "kimi-k2.5",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 131072,
                    "maxTokens": 32768,
                },
                {
                    "id": "quiet_sand",
                    "name": "quiet_sand",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 131072,
                    "maxTokens": 32768,
                },
            ],
        }

        default_model = MODEL_DEFAULTS.get(self.model_type)
        if default_model:
            config["agents"] = {
                "defaults": {
                    "model": default_model,
                    "thinkingDefault": "xhigh",
                }
            }

        with open(os.path.join(data_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        litellm_yaml = persona.litellm_config_yaml
        if not litellm_yaml:
            kimi_arn = env.get("KIMI_BEDROCK_MODEL_ARN", "").strip()
            kimi_region = env.get("KIMI_AWS_REGION", "us-east-1").strip()
            glm_arn = env.get("GLM_BEDROCK_MODEL_ARN", "").strip()
            glm_region = env.get("GLM_AWS_REGION", "us-east-1").strip()
            litellm_yaml = _DEFAULT_LITELLM_CONFIG.format(
                bedrock_arn=bedrock_arn or "PLACEHOLDER",
                aws_region=aws_region,
                kimi_bedrock_arn=kimi_arn or "PLACEHOLDER",
                kimi_aws_region=kimi_region,
                glm_bedrock_arn=glm_arn or "PLACEHOLDER",
                glm_aws_region=glm_region,
            )
        with open(os.path.join(workdir, "litellm-config.yaml"), "w") as f:
            f.write(litellm_yaml)

        gog_config_dir = os.path.join(workdir, "gog-config")
        os.makedirs(os.path.join(gog_config_dir, "gogcli", "keyring"), exist_ok=True)
        gog_auth_raw = self.talos_id.gog_auth
        gog_auth_token_raw = self.talos_id.gog_auth_token
        _logger.info(
            "[GogAuth→Docker] task=%s gog_auth present=%s length=%s gog_auth_token present=%s length=%s",
            self.talos_id.id,
            bool(gog_auth_raw),
            len(gog_auth_raw) if gog_auth_raw else 0,
            bool(gog_auth_token_raw),
            len(gog_auth_token_raw) if gog_auth_token_raw else 0,
        )

        # --- Write client_secret.json from gog_auth (client credentials only) ---
        if gog_auth_raw:
            try:
                gog_data = json.loads(gog_auth_raw)
                if isinstance(gog_data, dict):
                    client_secret_obj = None
                    if "client_secret" in gog_data and isinstance(
                        gog_data["client_secret"], dict
                    ):
                        client_secret_obj = gog_data["client_secret"]
                    elif "installed" in gog_data or "web" in gog_data:
                        client_secret_obj = gog_data

                    if client_secret_obj:
                        cs_path = os.path.join(
                            gog_config_dir, "gogcli", "client_secret.json"
                        )
                        with open(cs_path, "w") as f:
                            json.dump(client_secret_obj, f)
                        _logger.info(
                            "[GogAuth→Docker] wrote client_secret.json to %s", cs_path
                        )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth JSON for task %s",
                    self.talos_id.id,
                )

        # --- Write token/config files from gog_auth_token (auth tokens) ---
        if gog_auth_token_raw:
            try:
                token_data = json.loads(gog_auth_token_raw)
                if isinstance(token_data, dict):
                    gog_files = token_data.get("tokens", {})
                    written_files = []
                    for rel_path, content in gog_files.items():
                        if rel_path in ("client_secret", "tokens"):
                            continue
                        if not isinstance(content, str):
                            continue
                        abs_path = os.path.join(gog_config_dir, "gogcli", rel_path)
                        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                        with open(abs_path, "w") as f:
                            f.write(content)
                        written_files.append(rel_path)
                    _logger.info(
                        "[GogAuth→Docker] wrote %d token files to %s: %s",
                        len(written_files),
                        gog_config_dir,
                        written_files,
                    )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth_token JSON for task %s",
                    self.talos_id.id,
                )

        gog_cfg = os.path.join(gog_config_dir, "gogcli", "config.json")
        if not os.path.isfile(gog_cfg):
            with open(gog_cfg, "w") as f:
                json.dump({"keyring_backend": "file"}, f)

        nginx_conf = (
            "map $http_upgrade $connection_upgrade {\n"
            "    default upgrade;\n"
            "    ''      close;\n"
            "}\n"
            "server {\n"
            "    listen 80;\n"
            "    server_name _;\n"
            "    proxy_buffering off;\n"
            "    location /browser-api/ {\n"
            "        proxy_pass http://openclaw:18791/;\n"
            "        proxy_http_version 1.1;\n"
            '        proxy_set_header Authorization "Bearer %s";\n'
            "        proxy_set_header Host localhost;\n"
            "        proxy_read_timeout 30s;\n"
            "        proxy_send_timeout 30s;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_pass http://openclaw:18789;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection $connection_upgrade;\n"
            "        proxy_set_header Host localhost;\n"
            "        proxy_set_header Origin $http_origin;\n"
            "        proxy_set_header User-Agent $http_user_agent;\n"
            "        proxy_hide_header X-Frame-Options;\n"
            "        proxy_hide_header Content-Security-Policy;\n"
            "        proxy_read_timeout 600s;\n"
            "        proxy_send_timeout 600s;\n"
            "    }\n"
            "}\n"
        ) % gateway_token
        with open(os.path.join(workdir, "nginx.conf"), "w") as f:
            f.write(nginx_conf)

        override = (
            "services:\n"
            "  openclaw:\n"
            '    entrypoint: ["node", "openclaw.mjs", "gateway",'
            ' "--allow-unconfigured", "--token", "%s"]\n'
            "    command: []\n"
            "    ports: !override []\n"
            "    volumes:\n"
            "      - ./personas:/sandbox/personas:ro\n"
            "      - ./data/${PERSONA:-marcus}:/home/node/.openclaw\n"
            "      - ./gog-config:/home/node/.config:rw\n"
            "    environment:\n"
            "      - GOG_KEYRING_PASSWORD=${GOG_KEYRING_PASSWORD:-}\n"
            "      - GOG_ACCOUNT=${GOG_ACCOUNT:-}\n"
            "  nginx:\n"
            "    image: nginx:alpine\n"
            "    depends_on:\n"
            "      - openclaw\n"
            "    volumes:\n"
            "      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro\n"
            "    ports:\n"
            '      - "%d:80"\n'
            "    networks:\n"
            "      - frontend\n"
            "  litellm:\n"
            "    ports:\n"
            '      - "%d:4000"\n'
            "  db:\n"
            "    ports:\n"
            '      - "%d:5432"\n'
        ) % (gateway_token, gateway_port, litellm_port, db_port)
        with open(os.path.join(workdir, "docker-compose.override.yml"), "w") as f:
            f.write(override)

        return workdir

    def _build_compose_env(self, gateway_token):
        self.ensure_one()
        persona = self.talos_id.persona_id

        env = _load_dotenv().copy()
        env["PERSONA"] = persona.name
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        if not env.get("LITELLM_MASTER_KEY"):
            env["LITELLM_MASTER_KEY"] = "sk-talos-%s" % secrets.token_hex(8)

        gog_kp = self.talos_id.password or ""
        if gog_kp:
            env["GOG_KEYRING_PASSWORD"] = gog_kp

        task_email = self.talos_id.email
        if task_email:
            env["GOG_ACCOUNT"] = task_email

        _logger.info(
            "[GogAuth→Docker] _build_compose_env task=%s GOG_ACCOUNT=%s GOG_KEYRING_PASSWORD=%s",
            self.talos_id.id,
            task_email or "(none)",
            "***set***" if gog_kp else "(empty)",
        )
        return env

    def _wait_for_health(self, compose_bin, project_name, workdir):
        import urllib.request

        deadline = time.monotonic() + _HEALTH_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    compose_bin
                    + ["-p", project_name, "ps", "--format", "json", "openclaw"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    cwd=workdir,
                )
                output = result.stdout.strip()
                if output:
                    try:
                        data = json.loads(output)
                    except json.JSONDecodeError:
                        data = json.loads(output.splitlines()[0])

                    if isinstance(data, list):
                        data = data[0] if data else {}

                    state = (data.get("State") or "").lower()
                    if state in ("exited", "dead"):
                        _logger.warning(
                            "openclaw container exited (project=%s, state=%s)",
                            project_name,
                            state,
                        )
                        return False
            except (subprocess.TimeoutExpired, Exception) as e:
                _logger.debug("Health poll compose-ps error: %s", e)

            try:
                urllib.request.urlopen(
                    "http://localhost:%d/healthz" % self.docker_port,
                    timeout=5,
                )
                return True
            except Exception:
                pass

            time.sleep(_HEALTH_POLL_INTERVAL)

        return False

    def _capture_container_logs(self, compose_bin, project_name, workdir):
        try:
            result = subprocess.run(
                compose_bin + ["-p", project_name, "logs", "--tail", "30", "openclaw"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=workdir,
            )
            return result.stdout.strip() or result.stderr.strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Status reconciliation (local Docker)
    # ------------------------------------------------------------------

    def _check_local_status(self):
        """Check actual Docker container state and reconcile with DB status."""
        self.ensure_one()

        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                return

        if not self.docker_compose_project:
            if self.docker_status not in ("stopped",):
                self.write({"docker_status": "stopped"})
            return

        compose_bin = _compose_cmd()
        if not compose_bin:
            return

        workdir = self.docker_workdir
        try:
            cmd = compose_bin + [
                "-p",
                self.docker_compose_project,
                "ps",
                "--format",
                "json",
                "openclaw",
            ]
            kw = {
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            }
            if workdir and os.path.isdir(workdir):
                kw["cwd"] = workdir
            result = subprocess.run(cmd, **kw)

            output = result.stdout.strip()
            if not output:
                if self.docker_status == "starting":
                    # Containers may not exist yet (still building image).
                    # Leave as "starting" — the poll will check again later.
                    return
                if self.docker_status != "stopped":
                    _logger.info(
                        "[StatusCheck] No container found for project=%s sandbox=%s, marking stopped",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "stopped",
                            "docker_compose_project": False,
                            "docker_port": 0,
                            "docker_litellm_port": 0,
                            "docker_gateway_token": False,
                            "docker_workdir": False,
                            "docker_error": False,
                        }
                    )
                return

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                data = json.loads(output.splitlines()[0])

            if isinstance(data, list):
                data = data[0] if data else {}

            state = (data.get("State") or "").lower()
            health = (data.get("Health") or "").lower()

            if state in ("exited", "dead"):
                if self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container exited for project=%s sandbox=%s (state=%s), marking error",
                        self.docker_compose_project,
                        self.id,
                        state,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container exited unexpectedly (state=%s)"
                            % state,
                        }
                    )
            elif state == "running" and health == "unhealthy":
                if self.docker_status == "starting":
                    _logger.debug(
                        "[StatusCheck] Container unhealthy during startup for project=%s sandbox=%s, "
                        "waiting for health check to pass",
                        self.docker_compose_project,
                        self.id,
                    )
                elif self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container unhealthy for project=%s sandbox=%s, marking error",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container running but unhealthy",
                        }
                    )
            elif state == "running" and health in ("", "healthy"):
                if self.docker_status != "running":
                    _logger.info(
                        "[StatusCheck] Container running for project=%s sandbox=%s, updating to running",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write({"docker_status": "running"})
            elif state == "running" and health == "starting":
                # Docker health check still running — container is up but
                # not yet confirmed healthy.  Leave as "starting" so the
                # frontend poll checks again in a few seconds.
                _logger.debug(
                    "[StatusCheck] Container running, health starting for project=%s sandbox=%s",
                    self.docker_compose_project,
                    self.id,
                )
            # else: "created", "restarting" etc → leave as "starting"

        except subprocess.TimeoutExpired:
            _logger.debug(
                "[StatusCheck] Timed out checking status for sandbox %s", self.id
            )
        except Exception as e:
            _logger.debug(
                "[StatusCheck] Error checking status for sandbox %s: %s", self.id, e
            )

    def action_check_status(self):
        """Public action: reconcile DB docker_status with actual Docker state.

        Called by the frontend on page load and during polling to fix stale
        statuses.  Returns a dict mapping sandbox_id → current docker_status.
        """
        mode = self._deployment_mode()
        k8s = self.env["talos.sandbox.k8s"] if mode == "k8s" else None
        result = {}
        for sandbox in self:
            if (
                sandbox.docker_status in ("stopped",)
                and not sandbox.docker_compose_project
            ):
                result[sandbox.id] = sandbox.docker_status
                continue

            # Skip sandboxes that are actively being started in a background
            # thread — the thread will set the final status itself.
            with _SANDBOX_LOCK:
                if sandbox.id in _SANDBOX_STARTING:
                    result[sandbox.id] = sandbox.docker_status
                    continue

            if mode == "local":
                sandbox._check_local_status()
            elif mode == "k8s" and sandbox.docker_status in ("starting", "running"):
                try:
                    k8s_status = k8s.get_sandbox_status(sandbox)
                    if k8s_status != sandbox.docker_status:
                        vals = {"docker_status": k8s_status}
                        if k8s_status == "error":
                            vals["docker_error"] = (
                                "Sandbox deployment not found after timeout"
                            )
                        sandbox.write(vals)
                except Exception:
                    _logger.debug(
                        "[action_check_status] K8s status check failed for "
                        "sandbox %s, returning DB value",
                        sandbox.id,
                        exc_info=True,
                    )

            result[sandbox.id] = sandbox.docker_status
        return result

    # ------------------------------------------------------------------
    # Cron reconciliation (k8s)
    # ------------------------------------------------------------------

    @api.model
    def _cron_reconcile(self):
        mode = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
            .strip()
        )
        if mode != "k8s":
            return

        sandboxes = self.sudo().search(
            [("docker_status", "in", ["starting", "running"])]
        )
        if not sandboxes:
            return

        k8s = self.env["talos.sandbox.k8s"]
        for sandbox in sandboxes:
            try:
                status = k8s.get_sandbox_status(sandbox)
                if status != sandbox.docker_status:
                    sandbox.write({"docker_status": status})
                    if status == "error":
                        sandbox.write(
                            {
                                "docker_error": "Sandbox deployment not found after timeout",
                            }
                        )
                    # Notify UI of status change
                    if status in ("running", "error"):
                        partner = (
                            sandbox.employee_id.user_id.partner_id
                            or sandbox.talos_id.user_id.partner_id
                        )
                        if partner:
                            self.env["bus.bus"]._sendone(
                                partner,
                                "talos/sandbox_ready",
                                {
                                    "sandbox_id": sandbox.id,
                                    "docker_status": status,
                                    "error": sandbox.docker_error or "",
                                    "model_type": sandbox.model_type,
                                },
                            )
            except Exception as e:
                _logger.error(
                    "Reconciliation error for sandbox %s: %s",
                    sandbox.id,
                    e,
                )

    # ── Auto-process XML-RPC methods (called by consumer) ─────────────

    @api.model
    def auto_process_get_ws_info(self, sandbox_id):
        """Return WS connection details for a running sandbox."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        if sandbox.docker_status != "running":
            return {
                "error": "Sandbox is not running (status=%s)" % sandbox.docker_status
            }

        mode = sandbox._deployment_mode()
        if mode == "k8s":
            ws_host = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("talos.ws_router_host", "")
                .strip()
            )
            if ws_host:
                ws_url = "wss://%s/sandbox/%s/" % (ws_host, sandbox.id)
            else:
                ws_url = ""
        else:
            ws_url = (
                "ws://localhost:%d" % sandbox.docker_port if sandbox.docker_port else ""
            )

        if not ws_url:
            return {"error": "Cannot determine WS URL"}

        return {
            "ws_url": ws_url,
            "gateway_token": sandbox.docker_gateway_token or "",
            "sandbox_id": sandbox.id,
        }

    @api.model
    def auto_process_create_turn(
        self,
        sandbox_id,
        message,
        is_hint=False,
        is_auto_hint=False,
        auto_hint_iteration=0,
        auto_hint_group_id="",
    ):
        """Create a turn record. Mirrors create_turn controller logic."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        model_name = MODEL_DEFAULTS.get(sandbox.model_type, "unknown")
        next_num = len(sandbox.turn_ids) + 1
        is_hint_turn = bool(is_hint)

        vals = {
            "sandbox_id": sandbox.id,
            "turn_number": next_num,
            "model_name": model_name,
            "turn_status": "Pending",
            "is_hint_turn": is_hint_turn,
        }
        if is_hint_turn:
            vals["hints"] = message
        else:
            vals["prompt"] = message
        vals["prompt_timestamp"] = fields.Datetime.now()

        if is_auto_hint:
            vals["is_auto_hint"] = True
            vals["auto_hint_iteration"] = int(auto_hint_iteration or 0)
            if auto_hint_group_id:
                vals["auto_hint_group_id"] = auto_hint_group_id

        turn = self.env["talos.turn"].create(vals)

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @api.model
    def auto_process_save_response(
        self,
        turn_id,
        response,
        tool_calls_json="",
        partial=False,
    ):
        """Save response to a turn. Mirrors save_response controller logic."""
        turn = self.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {
            "response": response or "",
            "turn_status": "Streaming" if partial else "Completed",
        }
        if tool_calls_json:
            vals["tool_calls"] = tool_calls_json
        vals["response_timestamp"] = fields.Datetime.now()

        turn.write(vals)
        return {"success": True}

    @api.model
    def auto_process_save_trajectory(self, sandbox_id, turn_id, trajectory_json):
        """Save full trajectory JSON from chat.history to the turn."""
        turn = self.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        if trajectory_json:
            if isinstance(trajectory_json, list):
                trajectory_json = json.dumps(trajectory_json, ensure_ascii=False)
            turn.write({"trajectory_messages": trajectory_json})

        return {"success": True}

    @api.model
    def auto_process_trigger_hint_eval(self, turn_id, sandbox_id):
        """Trigger auto-hint evaluation. Same logic as /talos/auto_hint_eval endpoint."""
        import uuid

        from ..controllers.auto_hint import _AUTO_HINT_POOL, _auto_hint_eval_bg

        turn = self.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}
        if turn.turn_status != "Completed":
            return {"error": "Turn is not completed"}
        if not turn.response:
            return {"error": "Turn has no response"}

        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        current_iter = sandbox.auto_hint_iteration or 0
        if current_iter >= 5:
            return {"status": "max_retries"}

        group_id = sandbox.auto_hint_group_id or ""
        if current_iter == 0:
            group_id = uuid.uuid4().hex

        new_iter = current_iter + 1
        sandbox.write(
            {
                "auto_hint_status": "evaluating",
                "auto_hint_iteration": new_iter,
                "auto_hint_group_id": group_id,
            }
        )

        db_name = self.env.cr.dbname
        # Use admin partner for notifications (consumer is headless)
        notify_partner_id = self.env["res.users"].browse(SUPERUSER_ID).partner_id.id

        def _submit():
            _AUTO_HINT_POOL.submit(
                _auto_hint_eval_bg,
                db_name,
                sandbox_id,
                turn_id,
                group_id,
                new_iter,
                notify_partner_id,
            )

        self.env.cr.postcommit.add(_submit)

        return {"status": "pending", "iteration": new_iter, "group_id": group_id}

    @api.model
    def auto_process_poll_hint_status(self, sandbox_id):
        """Read current auto_hint_status and related data for polling."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        result = {
            "auto_hint_status": sandbox.auto_hint_status or "idle",
            "auto_hint_iteration": sandbox.auto_hint_iteration or 0,
            "auto_hint_group_id": sandbox.auto_hint_group_id or "",
        }

        # Find the last turn and its feedback
        last_turn = sandbox.turn_ids.sorted("turn_number", reverse=True)[:1]
        if last_turn:
            result["last_turn_id"] = last_turn.id
            result["last_turn_feedback"] = last_turn.feedback or ""
            result["last_turn_hint_text"] = last_turn.hint_text or ""
        else:
            result["last_turn_id"] = 0
            result["last_turn_feedback"] = ""
            result["last_turn_hint_text"] = ""

        return result

    @api.model
    def auto_process_save_feedback(self, turn_id, feedback, hint_text=""):
        """Save feedback on a turn. Mirrors save_feedback controller logic."""
        turn = self.env["talos.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        feedback = (feedback or "").strip().lower()
        if feedback not in ("satisfied", "unsatisfied"):
            return {"error": "Invalid feedback: %s" % feedback}

        vals = {"feedback": feedback}
        if hint_text:
            vals["hint_text"] = hint_text

        turn.write(vals)
        return {"success": True}

    @api.model
    def auto_process_reset_hint_status(self, sandbox_id):
        """Reset stuck auto_hint_status to idle (used on timeout)."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        sandbox.write(
            {
                "auto_hint_status": "idle",
                "auto_hint_iteration": 0,
                "auto_hint_group_id": False,
            }
        )
        return {"success": True}
