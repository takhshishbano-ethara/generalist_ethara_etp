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
import uuid
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

_GOLDEN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="atlas-golden")
_GOLDEN_GENERATING = set()
_GOLDEN_LOCK = threading.Lock()

_TASKDESC_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="atlas-taskdesc")
_TASKDESC_GENERATING = set()
_TASKDESC_LOCK = threading.Lock()

_golden_prompt_cache = None
_taskdesc_prompt_cache = None


def _get_golden_prompt():
    global _golden_prompt_cache
    if _golden_prompt_cache is not None:
        return _golden_prompt_cache
    mod_path = get_module_path("atlas")
    if not mod_path:
        return ""
    path = os.path.join(mod_path, "golden_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _golden_prompt_cache = f.read().strip()
    else:
        _golden_prompt_cache = ""
    return _golden_prompt_cache


def _get_taskdesc_prompt():
    global _taskdesc_prompt_cache
    if _taskdesc_prompt_cache is not None:
        return _taskdesc_prompt_cache
    mod_path = get_module_path("atlas")
    if not mod_path:
        return ""
    path = os.path.join(mod_path, "task_description_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _taskdesc_prompt_cache = f.read().strip()
    else:
        _taskdesc_prompt_cache = ""
    return _taskdesc_prompt_cache


def _run_golden_generation_background(db_name, task_id, notify_partner_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                _logger.error("Golden gen: task %s does not exist", task_id)
                return

            glm_traj = task.glm_trajectory or ""

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Atlas"
            )

        system_prompt = _get_golden_prompt()

        delivery_schema = ""
        schema_path = os.path.join(
            get_module_path("atlas") or "", "Delivery_Schema.json"
        )
        if os.path.isfile(schema_path):
            with open(schema_path, "r") as f:
                delivery_schema = f.read().strip()

        user_message = (
            "## Current Date\n%s\n\n"
            "## Delivery Schema\n```json\n%s\n```\n\n"
            "## Model Trajectory (GLM)\n%s"
        ) % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
            delivery_schema,
            glm_traj,
        )

        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=65536,
            temperature=0.7,
            timeout=1200.0,
        )
        _logger.info(
            "Golden trajectory generated for task %s (%d chars, tokens: %s)",
            task_id,
            len(response_text),
            usage,
        )

        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["atlas.atlas"].browse(task_id)
                    if not task.exists():
                        return
                    write_vals = {
                        "golden_trajectory": response_text,
                        "golden_status": "done",
                        "golden_error": False,
                        "golden_started_at": False,
                    }
                    g_in = usage.get("input_tokens", 0)
                    g_out = usage.get("output_tokens", 0)
                    if g_in > 0 or g_out > 0:
                        write_vals["golden_input_tokens"] = (
                            task.golden_input_tokens or 0
                        ) + g_in
                        write_vals["golden_output_tokens"] = (
                            task.golden_output_tokens or 0
                        ) + g_out
                    task.write(write_vals)
                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "atlas/golden_ready",
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
                task = env["atlas.atlas"].browse(task_id)
                if task.exists():
                    task.write(
                        {
                            "golden_status": "error",
                            "golden_error": str(e)[:1000],
                            "golden_started_at": False,
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
                            "atlas/golden_ready",
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


def _run_task_description_background(db_name, task_id, notify_partner_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                _logger.error("Task desc gen: task %s does not exist", task_id)
                return

            glm_traj = task.glm_trajectory or ""

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Atlas"
            )

        system_prompt = _get_taskdesc_prompt()

        user_message = (
            "## GLM Trajectory\n%s"
        ) % (glm_traj,)

        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=4096,
            temperature=0.5,
            timeout=300.0,
        )
        _logger.info(
            "Task description generated for task %s (%d chars, tokens: %s)",
            task_id,
            len(response_text),
            usage,
        )

        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["atlas.atlas"].browse(task_id)
                    if not task.exists():
                        return
                    write_vals = {
                        "task_description": response_text,
                        "task_description_status": "done",
                        "task_description_error": False,
                    }
                    t_in = usage.get("input_tokens", 0)
                    t_out = usage.get("output_tokens", 0)
                    if t_in > 0 or t_out > 0:
                        write_vals["taskdesc_input_tokens"] = (
                            task.taskdesc_input_tokens or 0
                        ) + t_in
                        write_vals["taskdesc_output_tokens"] = (
                            task.taskdesc_output_tokens or 0
                        ) + t_out
                    task.write(write_vals)
                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "atlas/taskdesc_ready",
                            {"task_id": task_id, "status": "done"},
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise

    except Exception as e:
        _logger.exception("Task description generation failed for task %s", task_id)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                task = env["atlas.atlas"].browse(task_id)
                if task.exists():
                    task.write(
                        {
                            "task_description_status": "error",
                            "task_description_error": str(e)[:1000],
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
                            "atlas/taskdesc_ready",
                            {
                                "task_id": task_id,
                                "status": "error",
                                "error": str(e)[:500],
                            },
                        )
        except Exception:
            _logger.exception(
                "Failed to write task desc error status for task %s", task_id
            )
    finally:
        with _TASKDESC_LOCK:
            _TASKDESC_GENERATING.discard(task_id)


import re as _re


def _is_degenerate_output(text):
    if not text or len(text) < 20:
        return True
    repeated = _re.search(r"(.)\1{15,}", text)
    if repeated:
        return True
    unique_chars = len(set(text.lower()))
    if unique_chars < 8 and len(text) > 30:
        return True
    return False


def generate_task_description_sync(env, messages_json):
    try:
        ICP = env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

        dotenv = _load_dotenv()
        api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key or not inference_arn:
            _logger.warning("generate_task_description_sync: missing credentials")
            return "", {}

        system_prompt = _get_taskdesc_prompt()
        if not system_prompt:
            _logger.warning(
                "generate_task_description_sync: no task_description_prompt.md"
            )
            return "", {}

        if isinstance(messages_json, list):
            messages_text = json.dumps(messages_json, ensure_ascii=False)[:16000]
        else:
            messages_text = str(messages_json)[:16000]

        user_message = ("## Chat Messages\n%s") % (messages_text,)

        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=1024,
            temperature=0.3,
            timeout=90.0,
        )
        desc = response_text.strip().replace("\n", " ")

        if _is_degenerate_output(desc):
            _logger.warning(
                "generate_task_description_sync: degenerate output detected (%d chars), discarding",
                len(desc),
            )
            return "", usage

        _logger.info(
            "generate_task_description_sync: generated %d chars, tokens=%s",
            len(desc),
            usage,
        )
        return desc, usage
    except Exception as e:
        _logger.warning("generate_task_description_sync failed: %s", e)
        return "", {}


def _format_tool_result(result):
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _wrap_trajectory_message(msg, is_accepted=0, hints=None):
    role = ""
    inner = msg.get("message", {})
    if isinstance(inner, dict):
        role = inner.get("role", "")
    if role in ("assistant", "toolResult"):
        return {
            "is_accepted": is_accepted,
            "hints": hints,
            "message": msg,
        }
    return msg


def _wrap_messages_with_turn_feedback(messages, turns):
    turn_list = list(turns)
    if not turn_list:
        return [_wrap_trajectory_message(m) for m in messages]

    turn_feedback = []
    for t in turn_list:
        user_text = (t.prompt or t.hints or "").strip()
        if t.hints:
            is_accepted = 1
            hint = (t.hints or "").strip()
        else:
            is_accepted = 0
            hint = None
        turn_feedback.append((user_text, is_accepted, hint))

    wrapped = []
    current_accepted = 0
    current_hints = None
    turn_idx = 0

    for msg in messages:
        inner = msg.get("message", {})
        role = inner.get("role", "") if isinstance(inner, dict) else ""

        if role == "user" and turn_idx < len(turn_feedback):
            content = inner.get("content", [])
            user_text = ""
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        user_text = (block.get("text") or "").strip()
                        break
            elif isinstance(content, str):
                user_text = content.strip()

            expected = turn_feedback[turn_idx][0]
            matched = False
            if user_text and expected:
                if user_text == expected:
                    matched = True
                elif user_text in expected or expected in user_text:
                    matched = True

            if matched:
                current_accepted = turn_feedback[turn_idx][1]
                current_hints = turn_feedback[turn_idx][2]
                turn_idx += 1
            elif user_text:
                current_accepted = turn_feedback[turn_idx][1]
                current_hints = turn_feedback[turn_idx][2]
                turn_idx += 1

        wrapped.append(_wrap_trajectory_message(msg, current_accepted, current_hints))

    return wrapped


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
  - model_name: glm-5
    litellm_params:
      model: bedrock/converse/{glm_bedrock_arn}
      aws_region_name: {glm_aws_region}
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
    mod_path = get_module_path("atlas")
    if not mod_path:
        return None
    return os.path.join(mod_path, "sandbox_docker")


class Atlas(models.Model):
    _name = "atlas.atlas"
    _description = "Atlas"

    is_atlas_admin = fields.Boolean(
        compute="_compute_is_atlas_admin",
        search="_search_is_atlas_admin",
    )

    @api.depends_context("uid")
    def _compute_is_atlas_admin(self):
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        for rec in self:
            rec.is_atlas_admin = is_admin

    def _search_is_atlas_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one(
        "hr.employee",
        default=lambda self: self.env.user.employee_id,
    )
    user_id = fields.Many2one(related="employee_id.user_id")

    email = fields.Char(string="Email")
    password = fields.Char(string="Password")
    gog_auth = fields.Text(string="Google Auth")
    gog_auth_token = fields.Text(string="Google Auth Token")

    sandbox_ids = fields.One2many("atlas.sandbox", "atlas_id", string="Sandboxes")
    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )

    glm_sandbox_id = fields.Many2one(
        "atlas.sandbox", compute="_compute_sandbox_ids", string="GLM Sandbox"
    )

    glm_status = fields.Selection(related="glm_sandbox_id.docker_status")
    glm_session_status = fields.Selection(related="glm_sandbox_id.session_status")

    glm_trajectory = fields.Text(string="GLM 5 Trajectory")
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
    golden_started_at = fields.Datetime(string="Golden Started At")

    task_description = fields.Text(string="Task Description")
    task_description_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Task Description Status",
        default="idle",
    )
    task_description_error = fields.Text(string="Task Description Error")

    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
    bedrock_input_tokens = fields.Integer(string="Bedrock QC Input Tokens", default=0)
    bedrock_output_tokens = fields.Integer(string="Bedrock QC Output Tokens", default=0)

    traj_qc_input_tokens = fields.Integer(string="Traj QC Input Tokens", default=0)
    traj_qc_output_tokens = fields.Integer(string="Traj QC Output Tokens", default=0)
    taskdesc_input_tokens = fields.Integer(string="Task Desc Input Tokens", default=0)
    taskdesc_output_tokens = fields.Integer(string="Task Desc Output Tokens", default=0)
    golden_input_tokens = fields.Integer(string="Golden Gen Input Tokens", default=0)
    golden_output_tokens = fields.Integer(string="Golden Gen Output Tokens", default=0)

    @api.depends("sandbox_ids", "sandbox_ids.model_type")
    def _compute_sandbox_ids(self):
        for rec in self:
            sandbox = rec.sandbox_ids.filtered(
                lambda s: s.model_type == "glm"
            )[:1]
            rec.glm_sandbox_id = sandbox.id if sandbox else False

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.ensure_sandboxes()
        return records

    def ensure_sandboxes(self):
        for rec in self:
            existing = rec.sandbox_ids.mapped("model_type")
            if "glm" not in existing:
                self.env["atlas.sandbox"].create(
                    {"atlas_id": rec.id, "model_type": "glm"}
                )

    def _get_all_turns(self):
        self.ensure_one()
        turns = self.env["atlas.turn"]
        for sandbox in self.sandbox_ids:
            turns |= sandbox.turn_ids
        return turns.sorted("turn_number")

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "atlas.turn",
            "view_mode": "list,form",
            "domain": [("sandbox_id", "in", self.sandbox_ids.ids)],
            "context": {"default_atlas_id": self.id},
        }

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/atlas/chat/export_session?task_id={self.id}",
            "target": "self",
        }

    def action_delete_trajectory_entry(self, field_name, entry_index):
        self.ensure_one()
        valid_fields = {
            "glm_trajectory",
            "golden_trajectory",
        }
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
        self.write(
            {
                field_name: json.dumps(entries, indent=2, ensure_ascii=False)
                if entries
                else ""
            }
        )
        return True

    def action_clear_turns(self):
        self.ensure_one()
        turns = self._get_all_turns()
        count = len(turns)
        turns.unlink()
        _logger.info("Cleared %d turns for task %s", count, self.id)

    def action_generate_golden_trajectory(self):
        self.ensure_one()
        if not self.glm_trajectory:
            raise UserError("GLM trajectory is empty. Stop the GLM sandbox first.")

        with _GOLDEN_LOCK:
            if self.id in _GOLDEN_GENERATING:
                raise UserError("Golden trajectory generation is already in progress.")
            _GOLDEN_GENERATING.add(self.id)

        self.write(
            {
                "golden_status": "generating",
                "golden_error": False,
                "golden_started_at": fields.Datetime.now(),
            }
        )

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

    def action_generate_task_description(self):
        self.ensure_one()
        if not self.glm_trajectory:
            raise UserError(
                "GLM trajectory is required to generate a task description."
            )

        with _TASKDESC_LOCK:
            if self.id in _TASKDESC_GENERATING:
                raise UserError("Task description generation is already in progress.")
            _TASKDESC_GENERATING.add(self.id)

        self.write(
            {"task_description_status": "generating", "task_description_error": False}
        )

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        @self.env.cr.postcommit.add
        def _queue_taskdesc():
            _TASKDESC_POOL.submit(
                _run_task_description_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    def build_trajectory_json(self):
        self.ensure_one()
        model_name = ""
        all_turns = self._get_all_turns().sorted("turn_number")
        for t in reversed(all_turns):
            if t.model_name:
                model_name = t.model_name
                break

        meta_info = {
            "task_completion_status": "success",
            "platform": "macOS",
            "model": model_name,
            "conv_id": str(uuid.uuid4()),
        }

        messages = self._trajectory_from_ws()
        if messages:
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = self._build_trajectory_fallback()

        return {"meta_info": meta_info, "messages": messages}

    def _trajectory_from_ws(self):
        self.ensure_one()
        best_messages = []
        best_count = 0
        for t in self._get_all_turns().sorted("turn_number", reverse=True):
            if t.trajectory_messages:
                try:
                    ws_messages = json.loads(t.trajectory_messages)
                    if isinstance(ws_messages, list) and len(ws_messages) > best_count:
                        best_messages = ws_messages
                        best_count = len(ws_messages)
                except (json.JSONDecodeError, TypeError):
                    continue
        return best_messages

    def _build_trajectory_fallback(self):
        self.ensure_one()
        messages = []
        msg_counter = 0
        parent_id = None
        prev_hint_text = None

        for t in self._get_all_turns():
            run_id = t.run_id or ""
            if t.is_hint_turn and prev_hint_text:
                is_accepted = 1
                hints = prev_hint_text
            else:
                is_accepted = 0
                hints = None
            prev_hint_text = t.hint_text or None

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
                        pre_count = len(messages)
                        messages, msg_counter, parent_id = (
                            self._build_trajectory_from_events(
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
            .get_param("atlas.deployment_mode", "local")
            .strip()
        )

    @api.model
    def _cron_reconcile_sandboxes(self):
        self.env["atlas.sandbox"]._cron_reconcile()


class AtlasTurn(models.Model):
    _name = "atlas.turn"
    _description = "Atlas Turn"
    _order = "turn_number asc, id asc"

    sandbox_id = fields.Many2one(
        "atlas.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    atlas_id = fields.Many2one(related="sandbox_id.atlas_id", store=True, readonly=True)
    employee_id = fields.Many2one(
        related="atlas_id.employee_id", store=True, readonly=True
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
    trajectory_input_tokens = fields.Integer(
        string="Trajectory Input Tokens", default=0
    )
    trajectory_output_tokens = fields.Integer(
        string="Trajectory Output Tokens", default=0
    )
    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
    tool_names = fields.Char(
        string="Tools Used", compute="_compute_tool_names", store=True
    )
    feedback = fields.Selection(
        [("satisfied", "Satisfied"), ("unsatisfied", "Unsatisfied")],
        string="Feedback",
    )
    hints = fields.Text(string="Hints")
    hint_text = fields.Text(string="Hint Text")
    is_hint_turn = fields.Boolean(string="Is Hint Turn", default=False)

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


