import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import time

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432

_HEALTH_WAIT_TIMEOUT = 1200
_HEALTH_POLL_INTERVAL = 3


def _format_tool_result(result):
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _load_dotenv():
    env = os.environ.copy()

    root = None
    conf_path = odoo_config.rcfile
    if conf_path:
        root = os.path.dirname(os.path.abspath(conf_path))
    if not root:
        root = os.getcwd()

    dotenv_path = os.path.join(root, ".env")
    if os.path.isfile(dotenv_path):
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                env[key] = value
        _logger.debug("Loaded .env from %s", dotenv_path)

    return env


_DEFAULT_LITELLM_CONFIG = """\
model_list:
  - model_name: claude-opus-4.6
    litellm_params:
      model: bedrock/converse/{bedrock_arn}
      aws_region_name: {aws_region}
      input_cost_per_token: 0.000005
      output_cost_per_token: 0.000025

  - model_name: kimi-k2.5
    litellm_params:
      model: bedrock/converse/{kimi_bedrock_arn}
      aws_region_name: {kimi_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: glm-5
    litellm_params:
      model: bedrock/converse/{glm_bedrock_arn}
      aws_region_name: {glm_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: kimi-k2.5
    litellm_params:
      model: bedrock/invoke/{kimi_bedrock_arn}
      aws_region_name: {kimi_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

litellm_settings:
  drop_params: true
  telemetry: false
  num_retries: 1
  request_timeout: 900
  stream_timeout: 60

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  store_model_in_db: true
"""


def _docker_available():
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose_cmd():
    for cmd in (["docker", "compose"], ["docker-compose"]):
        try:
            result = subprocess.run(
                cmd + ["version"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return cmd
        except FileNotFoundError:
            continue
    return None


def _module_sandbox_dir():
    mod_path = get_module_path("talos")
    if not mod_path:
        return None
    return os.path.join(mod_path, "sandbox_docker")


class Talos(models.Model):
    _name = "talos.talos"
    _description = "Talos"

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one("talos.domain", string="Parsona")
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one("hr.employee")
    user_id = fields.Many2one(related="employee_id.user_id")
    turn_ids = fields.One2many("talos.turn", "talos_id", string="Turns")
    turn_count = fields.Integer(string="Turns", compute="_compute_turn_count")

    persona_id = fields.Many2one(
        "talos.persona", string="Persona", required=True, ondelete="restrict"
    )
    docker_compose_project = fields.Char(
        string="Compose Project", readonly=True, copy=False
    )
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("error", "Error"),
        ],
        string="Docker Status",
        default="stopped",
        readonly=True,
    )
    docker_port = fields.Integer(string="Gateway Port", readonly=True)
    docker_litellm_port = fields.Integer(string="LiteLLM Port", readonly=True)
    docker_gateway_token = fields.Char(
        string="Gateway Token", readonly=True, copy=False
    )
    docker_dashboard_url = fields.Char(
        string="Dashboard URL", compute="_compute_dashboard_url"
    )
    docker_ws_url = fields.Char(
        string="Gateway WS URL", compute="_compute_docker_ws_url"
    )
    docker_error = fields.Text(string="Docker Error", readonly=True)
    docker_workdir = fields.Char(string="Working Directory", readonly=True, copy=False)
    heart_taxonomy = fields.Many2many("talos.taxonomy", string="HEART Taxonomy")
    task_type = fields.Selection(
        [("home_and_organization", "home_and_organization"), ("customer_service", "customer_service"), ("research_and_analysis", "research_and_analysis"), 
        ("creative_writing", "creative_writing"), ("technical_support", "technical_support"), ("education_and_learning", "education_and_learning"), 
        ("health_and_wellness", "health_and_wellness"), ("finance_and_budgeting", "finance_and_budgeting")], string="Task Type"
    )
    difficulty = fields.Selection(
        [("Single App", "Single App"), ("Multi App Light", "Multi App Light"), ("Multi App Complex", "Multi App Complex")], string="Difficulty"
    )
    trajectory_modifier = fields.Selection(
        [("Memory Usage", "Memory Usage"), ("Long Horizon Context", "Long Horizon Context"), ("Skill Discovery", "Skill Discovery"), 
        ("Claw Native Tools", "Claw Native Tools"), ("Skill Gap / Self-Extension", "Skill Gap / Self-Extension")], string="Trajectory Modifier"
    )
    safety_critical = fields.Selection(
        [("high_stake_actions", "high_stake_actions"), ("borderline_requests", "borderline_requests"), ("private_data_usage", "private_data_usage")], string="Safety Critical"
    )

    def _compute_turn_count(self):
        for rec in self:
            rec.turn_count = len(rec.turn_ids)

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "talos.turn",
            "view_mode": "list,form",
            "domain": [("talos_id", "=", self.id)],
            "context": {"default_talos_id": self.id},
        }

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/talos/chat/export_session?task_id={self.id}",
            "target": "self",
        }

    def build_trajectory_json(self):
        self.ensure_one()
        meta_info = {
            "task_type": self.task_type or "",
            "task_description": self.task_id or "",
            "task_completion_status": self.task_status or "",
            "platform": "macos",
            "persona": self.parsona.name if self.parsona else "",
            "persona_name": self.persona_id.name if self.persona_id else "",
            "difficulty": self.difficulty or "",
            "trajectory_modifier": self.trajectory_modifier or "",
            "safety_critical": self.safety_critical or "",
        }

        messages = self._try_trajectory_from_ws()
        if messages is None:
            messages = self._build_trajectory_fallback()

        return {"meta_info": meta_info, "messages": messages}

    def _try_trajectory_from_ws(self):
        self.ensure_one()
        for t in self.turn_ids.sorted("turn_number", reverse=True):
            if t.trajectory_messages:
                try:
                    ws_messages = json.loads(t.trajectory_messages)
                    if isinstance(ws_messages, list) and len(ws_messages) > 0:
                        return ws_messages
                except (json.JSONDecodeError, TypeError):
                    pass
        return None

    def _build_trajectory_fallback(self):
        self.ensure_one()
        messages = []
        msg_counter = 0
        task_id = self.id

        for t in self.turn_ids.sorted("turn_number"):
            parent_id = None

            if t.prompt:
                msg_counter += 1
                user_id = f"{task_id:08x}-{msg_counter:04x}"
                messages.append({
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "timestamp": t.create_date.isoformat() if t.create_date else "",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": t.prompt}],
                    },
                })
                parent_id = user_id

            if t.raw_events:
                try:
                    events = json.loads(t.raw_events)
                    if isinstance(events, list):
                        messages, msg_counter, parent_id = (
                            self._build_trajectory_from_events(
                                events, messages, msg_counter,
                                task_id, parent_id, t.model_name or "",
                            )
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

            elif t.response or t.tool_calls:
                if t.tool_calls:
                    try:
                        calls = json.loads(t.tool_calls)
                        if isinstance(calls, list):
                            for tc in calls:
                                msg_counter += 1
                                call_id = f"{task_id:08x}-{msg_counter:04x}"
                                tool_call_id = tc.get("toolCallId", call_id)
                                messages.append({
                                    "type": "message",
                                    "id": call_id,
                                    "parentId": parent_id,
                                    "timestamp": t.write_date.isoformat() if t.write_date else "",
                                    "message": {
                                        "role": "assistant",
                                        "content": [{
                                            "type": "toolCall",
                                            "id": tool_call_id,
                                            "name": tc.get("name", "unknown"),
                                            "arguments": tc.get("args", {}),
                                        }],
                                    },
                                })
                                parent_id = call_id

                                msg_counter += 1
                                result_id = f"{task_id:08x}-{msg_counter:04x}"
                                messages.append({
                                    "type": "message",
                                    "id": result_id,
                                    "parentId": parent_id,
                                    "timestamp": t.write_date.isoformat() if t.write_date else "",
                                    "message": {
                                        "role": "toolResult",
                                        "toolCallId": tool_call_id,
                                        "toolName": tc.get("name", "unknown"),
                                        "isError": tc.get("isError", False),
                                        "content": [{
                                            "type": "text",
                                            "text": _format_tool_result(tc.get("result")),
                                        }],
                                    },
                                })
                                parent_id = result_id
                    except (json.JSONDecodeError, TypeError):
                        pass

                if t.response:
                    msg_counter += 1
                    asst_id = f"{task_id:08x}-{msg_counter:04x}"
                    messages.append({
                        "type": "message",
                        "id": asst_id,
                        "parentId": parent_id,
                        "timestamp": t.write_date.isoformat() if t.write_date else "",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": t.response}],
                            "model": t.model_name or "",
                        },
                    })
                    parent_id = asst_id

        return messages

    @staticmethod
    def _build_trajectory_from_events(
        events, messages, msg_counter, task_id, parent_id, model_name
    ):
        pending_tool_calls = {}
        last_text = ""

        for ev in events:
            stream = ev.get("stream", "")
            data = ev.get("data", {})
            ts = ev.get("ts", "")

            if stream == "assistant" and data.get("text"):
                last_text = data["text"]

            elif stream == "tool":
                phase = data.get("phase", "")
                tcid = data.get("toolCallId", "")

                if phase == "start" and tcid:
                    if last_text:
                        msg_counter += 1
                        mid = f"{task_id:08x}-{msg_counter:04x}"
                        messages.append({
                            "type": "message",
                            "id": mid,
                            "parentId": parent_id,
                            "timestamp": ts,
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": last_text}],
                                "model": model_name,
                            },
                        })
                        parent_id = mid
                        last_text = ""

                    msg_counter += 1
                    call_msg_id = f"{task_id:08x}-{msg_counter:04x}"
                    messages.append({
                        "type": "message",
                        "id": call_msg_id,
                        "parentId": parent_id,
                        "timestamp": ts,
                        "message": {
                            "role": "assistant",
                            "content": [{
                                "type": "toolCall",
                                "id": tcid,
                                "name": data.get("name", "unknown"),
                                "arguments": data.get("args", {}),
                            }],
                        },
                    })
                    parent_id = call_msg_id
                    pending_tool_calls[tcid] = {
                        "name": data.get("name", "unknown"),
                        "result": None,
                        "isError": False,
                    }

                elif phase == "end" and tcid:
                    tc_info = pending_tool_calls.get(tcid, {})
                    msg_counter += 1
                    result_msg_id = f"{task_id:08x}-{msg_counter:04x}"
                    messages.append({
                        "type": "message",
                        "id": result_msg_id,
                        "parentId": parent_id,
                        "timestamp": ts,
                        "message": {
                            "role": "toolResult",
                            "toolCallId": tcid,
                            "toolName": tc_info.get("name", data.get("name", "unknown")),
                            "isError": bool(data.get("isError")),
                            "content": [{
                                "type": "text",
                                "text": _format_tool_result(
                                    data.get("result", data.get("partialResult"))
                                ),
                            }],
                        },
                    })
                    parent_id = result_msg_id
                    pending_tool_calls.pop(tcid, None)

            elif stream == "lifecycle" and data.get("phase") == "end":
                if last_text:
                    msg_counter += 1
                    mid = f"{task_id:08x}-{msg_counter:04x}"
                    messages.append({
                        "type": "message",
                        "id": mid,
                        "parentId": parent_id,
                        "timestamp": ts,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": last_text}],
                            "model": model_name,
                        },
                    })
                    parent_id = mid
                    last_text = ""

        if last_text:
            msg_counter += 1
            mid = f"{task_id:08x}-{msg_counter:04x}"
            messages.append({
                "type": "message",
                "id": mid,
                "parentId": parent_id,
                "timestamp": "",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": last_text}],
                    "model": model_name,
                },
            })
            parent_id = mid

        return messages, msg_counter, parent_id

    def _export_and_clear_turns(self):
        """Export trajectory as ir.attachment, clear turns, return attachment."""
        self.ensure_one()
        if not self.turn_ids:
            return self.env["ir.attachment"]

        trajectory = self.build_trajectory_json()
        content = json.dumps(trajectory, indent=2, ensure_ascii=False)
        filename = "session-%s.json" % self.id

        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(content.encode("utf-8")),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/json",
        })
        _logger.info(
            "Auto-exported trajectory (%d bytes, %d messages) for task %s",
            len(content), len(trajectory.get("messages", [])), self.id,
        )

        turn_count = len(self.turn_ids)
        self.turn_ids.unlink()
        _logger.info("Cleared %d turns for task %s", turn_count, self.id)

        return attachment

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

    def action_start_sandbox(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            self._start_k8s()
        else:
            self._start_local()

    def action_stop_sandbox(self):
        self.ensure_one()
        attachment = self._export_and_clear_turns()
        mode = self._deployment_mode()
        if mode == "k8s":
            self._stop_k8s()
        else:
            self._stop_local()

        if attachment:
            return {
                "type": "ir.actions.act_url",
                "url": "/web/content/%d?download=true" % attachment.id,
                "target": "self",
            }

    @api.model
    def _cron_reconcile_sandboxes(self):
        mode = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
            .strip()
        )
        if mode == "k8s":
            self.env["talos.sandbox.k8s"].reconcile_sandboxes()

    # ------------------------------------------------------------------
    # K8s methods (unchanged)
    # ------------------------------------------------------------------

    def _start_k8s(self):
        if self.docker_status == "running":
            raise UserError("Sandbox is already running for this task.")

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
                "Deployed K8s sandbox %s for task %s (persona=%s)",
                svc_name,
                self.id,
                self.persona_id.name,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for task %s: %s", self.id, e)
            self.write({"docker_status": "error", "docker_error": str(e)[:1000]})

    def _stop_k8s(self):
        if self.docker_status == "stopped":
            return

        try:
            self.env["talos.sandbox.k8s"].destroy_sandbox(self)
            _logger.info("Destroyed K8s sandbox for task %s", self.id)
        except Exception as e:
            _logger.warning("K8s sandbox destroy failed for task %s: %s", self.id, e)

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

    # ------------------------------------------------------------------
    # Local (Docker Compose) methods
    # ------------------------------------------------------------------

    def _allocate_ports(self):
        self.ensure_one()
        offset = self.id % 1000
        return (
            GATEWAY_PORT_BASE + offset,
            LITELLM_PORT_BASE + offset,
            DB_PORT_BASE + offset,
        )

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
            tempfile.gettempdir(), "talos-sandbox", "talos-%d" % self.id
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
                        "maxTokens": 8192,
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
                        "id": "claude-opus-4.6",
                        "name": "claude-opus-4.6",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 200000,
                        "maxTokens": 8192,
                    },
                    {
                        "id": "glm-5",
                        "name": "glm-5",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 131072,
                        "maxTokens": 8192,
                    },
                ],
            }
            config["agents"] = {"defaults": {"model": "litellm/claude-opus-4.6"}}

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

        nginx_conf = (
            "map $http_upgrade $connection_upgrade {\n"
            "    default upgrade;\n"
            "    ''      close;\n"
            "}\n"
            "server {\n"
            "    listen 80;\n"
            "    server_name _;\n"
            "    proxy_buffering off;\n"
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
        )
        with open(os.path.join(workdir, "nginx.conf"), "w") as f:
            f.write(nginx_conf)

        override = (
            "services:\n"
            "  openclaw:\n"
            '    entrypoint: ["node", "openclaw.mjs", "gateway",'
            ' "--allow-unconfigured", "--token", "%s"]\n'
            "    command: []\n"
            "    ports: !override []\n"
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
        persona = self.persona_id

        env = _load_dotenv().copy()
        env["PERSONA"] = persona.name
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        if not env.get("LITELLM_MASTER_KEY"):
            env["LITELLM_MASTER_KEY"] = "sk-talos-%s" % secrets.token_hex(8)

        return env

    def _wait_for_health(self, compose_bin, project_name, workdir):
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
                if not output:
                    time.sleep(_HEALTH_POLL_INTERVAL)
                    continue

                try:
                    data = json.loads(output)
                except json.JSONDecodeError:
                    data = json.loads(output.splitlines()[0])

                if isinstance(data, list):
                    data = data[0] if data else {}

                state = (data.get("State") or "").lower()
                health = (data.get("Health") or "").lower()

                if state in ("exited", "dead"):
                    _logger.warning(
                        "openclaw container exited (project=%s, state=%s)",
                        project_name,
                        state,
                    )
                    return False

                if health == "healthy" or state == "running":
                    try:
                        import urllib.request

                        urllib.request.urlopen(
                            "http://localhost:%d/healthz" % self.docker_port,
                            timeout=5,
                        )
                        return True
                    except Exception:
                        pass

            except (subprocess.TimeoutExpired, Exception) as e:
                _logger.debug("Health poll error: %s", e)

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

    def _start_local(self):
        if not self.env.user.has_group("talos.group_talos_admin"):
            raise UserError("Local mode is restricted to Talos administrators.")

        if self.docker_status == "running" and self.docker_compose_project:
            raise UserError("Docker stack is already running for this task.")

        if not _docker_available():
            raise UserError(
                "Docker is not available on this server. "
                "Please ensure the Docker daemon is running."
            )

        compose_bin = _compose_cmd()
        if not compose_bin:
            raise UserError("docker compose (or docker-compose) not found.")

        persona = self.persona_id
        if not persona:
            raise UserError("No persona selected for this task.")

        gateway_token = secrets.token_hex(32)
        project_name = "talos-%d" % self.id
        gateway_port, litellm_port, db_port = self._allocate_ports()

        self.write({"docker_status": "starting", "docker_error": False})

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
                    "docker_status": "starting",
                    "docker_port": gateway_port,
                    "docker_litellm_port": litellm_port,
                    "docker_gateway_token": gateway_token,
                    "docker_workdir": workdir,
                    "docker_error": False,
                }
            )

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) task=%s persona=%s",
                    project_name,
                    self.id,
                    persona.name,
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
                    "Gateway health-check failed for project %s (task %s)",
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
                    "Stopped sandbox (project=%s) task=%s", project_name, self.id
                )
            except Exception as e:
                _logger.warning(
                    "Failed to stop compose project %s: %s", project_name, e
                )
        elif compose_bin and project_name:
            try:
                subprocess.run(
                    compose_bin
                    + ["-p", project_name, "down", "--volumes", "--remove-orphans"],
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


class TalosTurn(models.Model):
    _name = "talos.turn"
    _description = "Talos Turn"
    _order = "turn_number desc, id desc"

    talos_id = fields.Many2one("talos.talos", string="Talos", ondelete="cascade")
    turn_number = fields.Integer(string="Turn Number")
    turn_status = fields.Selection([("Pending", "Pending"), ("Completed", "Completed")])
    prompt = fields.Text(string="Prompt")
    response = fields.Text(string="Response")
    run_id = fields.Char(string="Run ID", index=True)
    model_name = fields.Char(string="Model")
    tool_calls = fields.Text(string="Tool Calls (JSON)")
    raw_events = fields.Text(string="Raw WS Events (JSON)")
    trajectory_messages = fields.Text(string="Trajectory Messages (JSON)")
    tool_names = fields.Char(
        string="Tools Used", compute="_compute_tool_names", store=True
    )

    @api.depends("tool_calls")
    def _compute_tool_names(self):
        for rec in self:
            names = []
            if rec.tool_calls:
                try:
                    calls = json.loads(rec.tool_calls)
                    if isinstance(calls, list):
                        for c in calls:
                            n = c.get("name", "")
                            if n and n not in names:
                                names.append(n)
                except (json.JSONDecodeError, TypeError):
                    pass
            rec.tool_names = ", ".join(names) if names else False


class TalosTaxonomy(models.Model):
    _name = "talos.taxonomy"
    _description = "Talos Taxonomy"

    name = fields.Char(string="Name", required=True, unique=True)
