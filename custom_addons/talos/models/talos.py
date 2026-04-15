import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from odoo import models, fields, api, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432

_HEALTH_WAIT_TIMEOUT = 1200
_HEALTH_POLL_INTERVAL = 3

_GOLDEN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="talos-golden")
_GOLDEN_GENERATING = set()
_GOLDEN_LOCK = threading.Lock()

_golden_prompt_cache = None


def _get_golden_prompt():
    global _golden_prompt_cache
    if _golden_prompt_cache is not None:
        return _golden_prompt_cache
    mod_path = get_module_path("talos")
    if not mod_path:
        return ""
    path = os.path.join(mod_path, "golden_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _golden_prompt_cache = f.read().strip()
    else:
        _golden_prompt_cache = ""
    return _golden_prompt_cache


def _run_golden_generation_background(db_name, task_id, notify_partner_id):
    try:
        # Phase 1: read all inputs
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["talos.talos"].browse(task_id)
            if not task.exists():
                _logger.error("Golden gen: task %s does not exist", task_id)
                return

            claude_traj = task.claude_trajectory or ""
            glm_traj = task.glm_trajectory or ""
            persona = task.persona_id
            soul_md = persona.soul_md or "" if persona else ""
            memory_md = persona.memory_md or "" if persona else ""
            agents_md = persona.agents_md or "" if persona else ""

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Talos"
            )

        system_prompt = _get_golden_prompt()

        delivery_schema = ""
        schema_path = os.path.join(
            get_module_path("talos") or "", "Delivery_Schema.json"
        )
        if os.path.isfile(schema_path):
            with open(schema_path, "r") as f:
                delivery_schema = f.read().strip()

        user_message = (
            "## Current Date\n%s\n\n"
            "## Delivery Schema\n```json\n%s\n```\n\n"
            "## SOUL.md\n%s\n\n"
            "## MEMORY.md\n%s\n\n"
            "## AGENTS.md\n%s\n\n"
            "## Model Trajectory 1 (Claude)\n%s\n\n"
            "## Model Trajectory 2 (GLM)\n%s"
        ) % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
            delivery_schema,
            soul_md,
            memory_md,
            agents_md,
            claude_traj,
            glm_traj,
        )

        # Phase 2: call Bedrock (long-running, no cursor held)
        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=65536,
            temperature=0.7,
            timeout=600.0,
        )
        _logger.info(
            "Golden trajectory generated for task %s (%d chars, tokens: %s)",
            task_id,
            len(response_text),
            usage,
        )

        # Phase 3: write result + notify
        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["talos.talos"].browse(task_id)
                    if not task.exists():
                        return
                    task.write(
                        {
                            "golden_trajectory": response_text,
                            "golden_status": "done",
                            "golden_error": False,
                        }
                    )
                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "talos/golden_ready",
                            {"task_id": task_id, "status": "done"},
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise

    except Exception as e:
        _logger.exception("Golden trajectory generation failed for task %s", task_id)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                task = env["talos.talos"].browse(task_id)
                if task.exists():
                    task.write(
                        {
                            "golden_status": "error",
                            "golden_error": str(e)[:1000],
                        }
                    )
                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "talos/golden_ready",
                            {
                                "task_id": task_id,
                                "status": "error",
                                "error": str(e)[:500],
                            },
                        )
        except Exception:
            _logger.exception(
                "Failed to write golden error status for task %s", task_id
            )
    finally:
        with _GOLDEN_LOCK:
            _GOLDEN_GENERATING.discard(task_id)


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
      reasoning_effort: "high"
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
      reasoning_effort: "high"
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: kimi-k2.5
    litellm_params:
      model: bedrock/invoke/{kimi_bedrock_arn}
      aws_region_name: {kimi_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: quiet_sand
    litellm_params:
      model: openai/quiet_sand
      api_base: https://api.llama.com/v1alpha
      api_key: os.environ/LLAMA_API_KEY

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

    is_talos_admin = fields.Boolean(
        compute="_compute_is_talos_admin",
        search="_search_is_talos_admin",
    )

    @api.depends_context("uid")
    def _compute_is_talos_admin(self):
        is_admin = self.env.user.has_group("talos.group_talos_admin")
        for rec in self:
            rec.is_talos_admin = is_admin

    def _search_is_talos_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("talos.group_talos_admin")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one("talos.domain", string="Parsona")
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one("hr.employee")
    user_id = fields.Many2one(related="employee_id.user_id")

    persona_id = fields.Many2one(
        "talos.persona", string="Persona", required=True, ondelete="restrict"
    )
    heart_taxonomy = fields.Many2many("talos.taxonomy", string="HEART Taxonomy")
    task_type = fields.Selection(
        [
            ("home_and_organization", "home_and_organization"),
            ("customer_service", "customer_service"),
            ("research_and_analysis", "research_and_analysis"),
            ("creative_writing", "creative_writing"),
            ("technical_support", "technical_support"),
            ("education_and_learning", "education_and_learning"),
            ("health_and_wellness", "health_and_wellness"),
            ("finance_and_budgeting", "finance_and_budgeting"),
            ("sustainable_planning", "sustainable_planning"),
            ("historical_archiving", "historical_archiving"),
        ],
        string="Task Type",
    )
    difficulty = fields.Selection(
        [
            ("single_app", "Single App"),
            ("multi_app_light", "Multi App Light"),
            ("multi_app_complex", "Multi App Complex"),
        ],
        string="Difficulty",
    )
    trajectory_modifier = fields.Selection(
        [
            ("memory_usage", "Memory Usage"),
            ("long_horizon_context", "Long Horizon Context"),
            ("skill_discovery", "Skill Discovery"),
            ("claw_native_tools", "Claw Native Tools"),
            ("skill_gap_self_extension", "Skill Gap / Self-Extension"),
        ],
        string="Trajectory Modifier",
    )
    safety_critical = fields.Selection(
        [
            ("high_stake_actions", "high_stake_actions"),
            ("borderline_requests", "borderline_requests"),
            ("private_data_usage", "private_data_usage"),
        ],
        string="Safety Critical",
    )
    seed_prompt = fields.Text(string="Seed Prompt")
    agent_md = fields.Text(string="Agent MD")
    soul_md = fields.Text(string="Soul MD")
    memory_md = fields.Text(string="Memory MD")
    email = fields.Char(string="Email")
    password = fields.Char(string="Password")
    gog_auth = fields.Text(string="Google Auth")

    # Sandboxes
    sandbox_ids = fields.One2many("talos.sandbox", "talos_id", string="Sandboxes")
    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )

    # Computed convenience fields — one shortcut per model type
    claude_sandbox_id = fields.Many2one(
        "talos.sandbox", compute="_compute_sandbox_ids", string="Claude Sandbox"
    )
    glm_sandbox_id = fields.Many2one(
        "talos.sandbox", compute="_compute_sandbox_ids", string="GLM Sandbox"
    )
    oneP_sandbox_id = fields.Many2one(
        "talos.sandbox", compute="_compute_sandbox_ids", string="1P Sandbox"
    )

    claude_status = fields.Selection(related="claude_sandbox_id.docker_status")
    glm_status = fields.Selection(related="glm_sandbox_id.docker_status")
    oneP_status = fields.Selection(related="oneP_sandbox_id.docker_status")

    claude_session_status = fields.Selection(related="claude_sandbox_id.session_status")
    glm_session_status = fields.Selection(related="glm_sandbox_id.session_status")
    oneP_session_status = fields.Selection(related="oneP_sandbox_id.session_status")

    claude_trajectory = fields.Text(string="Claude 4.6 Trajectory")
    glm_trajectory = fields.Text(string="GLM 5 Trajectory")
    oneP_trajectory = fields.Text(string="1P Trajectory")
    golden_trajectory = fields.Text(string="Golden Trajectory")
    golden_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Golden Status",
        default="idle",
    )
    golden_error = fields.Text(string="Golden Error")

    @api.depends("sandbox_ids", "sandbox_ids.model_type")
    def _compute_sandbox_ids(self):
        for rec in self:
            for mtype, field in [
                ("claude", "claude_sandbox_id"),
                ("glm", "glm_sandbox_id"),
                ("1p", "oneP_sandbox_id"),
            ]:
                sandbox = rec.sandbox_ids.filtered(
                    lambda s, mt=mtype: s.model_type == mt
                )[:1]
                setattr(rec, field, sandbox.id if sandbox else False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._ensure_sandboxes()
        return records

    def _ensure_sandboxes(self):
        for rec in self:
            existing = rec.sandbox_ids.mapped("model_type")
            for mtype in ("claude", "glm", "1p"):
                if mtype not in existing:
                    self.env["talos.sandbox"].create(
                        {"talos_id": rec.id, "model_type": mtype}
                    )

    # ── Turns helper (aggregates across all sandboxes) ──────────

    def _get_all_turns(self):
        self.ensure_one()
        turns = self.env["talos.turn"]
        for sandbox in self.sandbox_ids:
            turns |= sandbox.turn_ids
        return turns.sorted("turn_number")

    # ── Actions ─────────────────────────────────────────────────

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "talos.turn",
            "view_mode": "list,form",
            "domain": [("sandbox_id", "in", self.sandbox_ids.ids)],
            "context": {"default_talos_id": self.id},
        }

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/talos/chat/export_session?task_id={self.id}",
            "target": "self",
        }

    def action_delete_trajectory_entry(self, field_name, entry_index):
        self.ensure_one()
        valid_fields = {"claude_trajectory", "glm_trajectory", "oneP_trajectory", "golden_trajectory"}
        if field_name not in valid_fields:
            raise UserError(f"Invalid trajectory field: {field_name}")

        raw = self[field_name] or ""
        if not raw.strip():
            return False

        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise UserError("Trajectory data is corrupted.")

        if not isinstance(entries, list):
            raise UserError("Trajectory data is not in multi-session format.")

        if entry_index < 0 or entry_index >= len(entries):
            raise UserError(f"Invalid entry index: {entry_index}")

        entries.pop(entry_index)
        self.write({field_name: json.dumps(entries, indent=2, ensure_ascii=False) if entries else ""})
        return True

    def action_clear_turns(self):
        self.ensure_one()
        turns = self._get_all_turns()
        count = len(turns)
        turns.unlink()
        _logger.info("Cleared %d turns for task %s", count, self.id)

    def action_generate_golden_trajectory(self):
        self.ensure_one()
        if not self.claude_trajectory:
            raise UserError(
                "Claude trajectory is empty. Stop the Claude sandbox first."
            )
        if not self.glm_trajectory:
            raise UserError("GLM trajectory is empty. Stop the GLM sandbox first.")
        if not self.persona_id:
            raise UserError("No persona selected.")

        with _GOLDEN_LOCK:
            if self.id in _GOLDEN_GENERATING:
                raise UserError("Golden trajectory generation is already in progress.")
            _GOLDEN_GENERATING.add(self.id)

        self.write({"golden_status": "generating", "golden_error": False})

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        @self.env.cr.postcommit.add
        def _queue():
            _GOLDEN_POOL.submit(
                _run_golden_generation_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    # ── Trajectory export ───────────────────────────────────────

    def build_trajectory_json(self):
        self.ensure_one()
        model_name = ""
        for t in self._get_all_turns().sorted("turn_number", reverse=True):
            if t.model_name:
                model_name = t.model_name
                break

        meta_info = {
            "task_type": self.task_type or "",
            "task_description": self.task_id or "",
            "task_completion_status": self.task_status or "",
            "system_prompt": self.seed_prompt or "",
            "platform": "macOS",
            "persona": self.persona_id.name if self.persona_id else "",
            "model": model_name,
            "difficulty": self.difficulty or "",
        }

        messages = self._trajectory_from_ws()
        if not messages:
            messages = self._build_trajectory_fallback()

        return {"meta_info": meta_info, "messages": messages}

    def _trajectory_from_ws(self):
        self.ensure_one()
        for t in self._get_all_turns().sorted("turn_number", reverse=True):
            if t.trajectory_messages:
                try:
                    ws_messages = json.loads(t.trajectory_messages)
                    if isinstance(ws_messages, list) and len(ws_messages) > 0:
                        return ws_messages
                except (json.JSONDecodeError, TypeError):
                    pass
        return []

    def _build_trajectory_fallback(self):
        self.ensure_one()
        messages = []
        msg_counter = 0
        parent_id = None

        for t in self._get_all_turns():
            run_id = t.run_id or ""

            def _next_id():
                nonlocal msg_counter
                msg_counter += 1
                return "%s:%d" % (run_id, msg_counter) if run_id else ""

            if t.prompt:
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
                            "content": [{"type": "text", "text": t.prompt}],
                        },
                    }
                )
                parent_id = user_id

            if t.raw_events:
                try:
                    events = json.loads(t.raw_events)
                    if isinstance(events, list) and events:
                        messages, msg_counter, parent_id = (
                            self._build_trajectory_from_events(
                                events,
                                messages,
                                msg_counter,
                                parent_id,
                                t.model_name or "",
                            )
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
                            messages.append(
                                {
                                    "type": "message",
                                    "id": call_id,
                                    "parentId": parent_id,
                                    "timestamp": t.response_timestamp
                                    or (
                                        t.write_date.isoformat() if t.write_date else ""
                                    ),
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
                            )
                            parent_id = call_id

                            result_id = ("%s:result" % tcid) if tcid else _next_id()
                            messages.append(
                                {
                                    "type": "message",
                                    "id": result_id,
                                    "parentId": parent_id,
                                    "timestamp": t.response_timestamp
                                    or (
                                        t.write_date.isoformat() if t.write_date else ""
                                    ),
                                    "message": {
                                        "role": "toolResult",
                                        "toolCallId": tcid or call_id,
                                        "toolName": tc.get("name", "unknown"),
                                        "isError": tc.get("isError", False),
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": _format_tool_result(
                                                    tc.get("result")
                                                ),
                                            }
                                        ],
                                    },
                                }
                            )
                            parent_id = result_id
                except (json.JSONDecodeError, TypeError):
                    pass

            if t.response:
                asst_id = _next_id()
                messages.append(
                    {
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
                )
                parent_id = asst_id

        return messages

    @staticmethod
    def _build_trajectory_from_events(
        events, messages, msg_counter, parent_id, model_name
    ):
        pending_tool_calls = {}
        last_text = ""
        run_id = ""

        for ev in events:
            rid = ev.get("runId", "")
            if rid:
                run_id = rid
                break

        def _next_id():
            nonlocal msg_counter
            msg_counter += 1
            return "%s:%d" % (run_id, msg_counter) if run_id else ""

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
                        mid = _next_id()
                        messages.append(
                            {
                                "type": "message",
                                "id": mid,
                                "parentId": parent_id,
                                "timestamp": ts,
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": last_text}],
                                    "model": model_name,
                                },
                            }
                        )
                        parent_id = mid
                        last_text = ""

                    messages.append(
                        {
                            "type": "message",
                            "id": tcid,
                            "parentId": parent_id,
                            "timestamp": ts,
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": tcid,
                                        "name": data.get("name", "unknown"),
                                        "arguments": data.get("args", {}),
                                    }
                                ],
                            },
                        }
                    )
                    parent_id = tcid
                    pending_tool_calls[tcid] = {
                        "name": data.get("name", "unknown"),
                    }

                elif phase == "end" and tcid:
                    tc_info = pending_tool_calls.pop(tcid, {})
                    result_id = "%s:result" % tcid
                    messages.append(
                        {
                            "type": "message",
                            "id": result_id,
                            "parentId": parent_id,
                            "timestamp": ts,
                            "message": {
                                "role": "toolResult",
                                "toolCallId": tcid,
                                "toolName": tc_info.get(
                                    "name", data.get("name", "unknown")
                                ),
                                "isError": bool(data.get("isError")),
                                "content": [
                                    {
                                        "type": "text",
                                        "text": _format_tool_result(
                                            data.get(
                                                "result", data.get("partialResult")
                                            )
                                        ),
                                    }
                                ],
                            },
                        }
                    )
                    parent_id = result_id

            elif stream == "lifecycle" and data.get("phase") == "end":
                if last_text:
                    mid = _next_id()
                    messages.append(
                        {
                            "type": "message",
                            "id": mid,
                            "parentId": parent_id,
                            "timestamp": ts,
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": last_text}],
                                "model": model_name,
                            },
                        }
                    )
                    parent_id = mid
                    last_text = ""

        if last_text:
            mid = _next_id()
            messages.append(
                {
                    "type": "message",
                    "id": mid,
                    "parentId": parent_id,
                    "timestamp": "",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": last_text}],
                        "model": model_name,
                    },
                }
            )
            parent_id = mid

        return messages, msg_counter, parent_id

    def _export_and_clear_turns(self):
        """Export trajectory as ir.attachment, clear turns, return attachment."""
        self.ensure_one()
        all_turns = self._get_all_turns()
        if not all_turns:
            return self.env["ir.attachment"]

        trajectory = self.build_trajectory_json()
        content = json.dumps(trajectory, indent=2, ensure_ascii=False)
        filename = "session-%s.json" % self.id

        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "type": "binary",
                "datas": base64.b64encode(content.encode("utf-8")),
                "res_model": self._name,
                "res_id": self.id,
                "mimetype": "application/json",
            }
        )
        _logger.info(
            "Auto-exported trajectory (%d bytes, %d messages) for task %s",
            len(content),
            len(trajectory.get("messages", [])),
            self.id,
        )

        turn_count = len(all_turns)
        all_turns.unlink()
        _logger.info("Cleared %d turns for task %s", turn_count, self.id)

        return attachment

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
            .strip()
        )

    @api.model
    def _cron_reconcile_sandboxes(self):
        self.env["talos.sandbox"]._cron_reconcile()


class TalosTurn(models.Model):
    _name = "talos.turn"
    _description = "Talos Turn"
    _order = "turn_number asc, id asc"

    sandbox_id = fields.Many2one(
        "talos.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    talos_id = fields.Many2one(related="sandbox_id.talos_id", store=True, readonly=True)
    employee_id = fields.Many2one(
        related="talos_id.employee_id", store=True, readonly=True
    )
    turn_number = fields.Integer(string="Turn Number")
    turn_status = fields.Selection([("Pending", "Pending"), ("Completed", "Completed")])
    prompt = fields.Text(string="Prompt")
    response = fields.Text(string="Response")
    run_id = fields.Char(string="Run ID", index=True)
    model_name = fields.Char(string="Model")
    prompt_timestamp = fields.Char(string="Prompt Timestamp (ISO)")
    response_timestamp = fields.Char(string="Response Timestamp (ISO)")
    tool_calls = fields.Text(string="Tool Calls (JSON)")
    raw_events = fields.Text(string="Raw WS Events (JSON)")
    trajectory_messages = fields.Text(string="Trajectory Messages (JSON)")
    qc_severity = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        string="QC Severity",
    )
    qc_response = fields.Text(string="QC Response (JSON)")
    qc_dismiss_reason = fields.Text(string="QC Dismiss Reason")
    bedrock_input_tokens = fields.Integer(string="Bedrock QC Input Tokens", default=0)
    bedrock_output_tokens = fields.Integer(string="Bedrock QC Output Tokens", default=0)
    trajectory_input_tokens = fields.Integer(string="Trajectory Input Tokens", default=0)
    trajectory_output_tokens = fields.Integer(string="Trajectory Output Tokens", default=0)
    claude_input_tokens = fields.Integer(string="Claude Input Tokens", default=0)
    claude_output_tokens = fields.Integer(string="Claude Output Tokens", default=0)
    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
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
