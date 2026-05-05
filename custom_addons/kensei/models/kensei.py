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
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

GATEWAY_PORT_BASE = 21000
LITELLM_PORT_BASE = 16000
DB_PORT_BASE = 17432

_HEALTH_WAIT_TIMEOUT = 1200
_HEALTH_POLL_INTERVAL = 3

_GOLDEN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kensei-golden")
_GOLDEN_GENERATING = set()
_GOLDEN_LOCK = threading.Lock()

_TASKDESC_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kensei-taskdesc")
_TASKDESC_GENERATING = set()
_TASKDESC_LOCK = threading.Lock()

_golden_prompt_cache = None
_taskdesc_prompt_cache = None


def _get_golden_prompt():
    global _golden_prompt_cache
    if _golden_prompt_cache is not None:
        return _golden_prompt_cache
    mod_path = get_module_path("kensei")
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
    mod_path = get_module_path("kensei")
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
        # Phase 1: read all inputs
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei.kensei"].browse(task_id)
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
            inference_arn = (ICP.get_param("kensei.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("kensei.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Kensei"
            )

        system_prompt = _get_golden_prompt()

        delivery_schema = ""
        schema_path = os.path.join(
            get_module_path("kensei") or "", "Delivery_Schema.json"
        )
        if os.path.isfile(schema_path):
            with open(schema_path, "r") as f:
                delivery_schema = f.read().strip()

        trajectory_sections = []
        if claude_traj:
            trajectory_sections.append(
                "## Model Trajectory 1 (Claude)\n%s" % claude_traj
            )
        if glm_traj:
            idx = len(trajectory_sections) + 1
            trajectory_sections.append(
                "## Model Trajectory %d (GLM)\n%s" % (idx, glm_traj)
            )
        trajectories_block = "\n\n".join(trajectory_sections)

        user_message = (
            "## Current Date\n%s\n\n"
            "## Delivery Schema\n```json\n%s\n```\n\n"
            "## SOUL.md\n%s\n\n"
            "## MEMORY.md\n%s\n\n"
            "## AGENTS.md\n%s\n\n"
            "%s"
        ) % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
            delivery_schema,
            soul_md,
            memory_md,
            agents_md,
            trajectories_block,
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
            timeout=1200.0,
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
                    task = env["kensei.kensei"].browse(task_id)
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
                            "kensei/golden_ready",
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
                task = env["kensei.kensei"].browse(task_id)
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
                            "kensei/golden_ready",
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
            task = env["kensei.kensei"].browse(task_id)
            if not task.exists():
                _logger.error("Task desc gen: task %s does not exist", task_id)
                return

            claude_traj = task.claude_trajectory or ""
            glm_traj = task.glm_trajectory or ""
            oneP_traj = task.oneP_trajectory or ""
            seed_prompt = task.seed_prompt or ""
            persona = task.persona_id
            soul_md = persona.soul_md or "" if persona else ""

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("kensei.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("kensei.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key:
            raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Kensei"
            )

        system_prompt = _get_taskdesc_prompt()

        user_message = (
            "## Seed Prompt\n%s\n\n"
            "## Persona (SOUL.md)\n%s\n\n"
            "## Claude Trajectory\n%s\n\n"
            "## GLM Trajectory\n%s\n\n"
            "## 1P Trajectory\n%s"
        ) % (seed_prompt, soul_md, claude_traj, glm_traj, oneP_traj)

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
                    task = env["kensei.kensei"].browse(task_id)
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
                            "kensei/taskdesc_ready",
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
                task = env["kensei.kensei"].browse(task_id)
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
                            "kensei/taskdesc_ready",
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


def generate_task_description_sync(env, seed_prompt, messages_json):
    """Call Qwen/Bedrock to generate a single-line task description.

    Returns:
        Tuple of (description_string, usage_dict).
    """
    try:
        ICP = env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("kensei.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("kensei.bedrock_region") or "ap-south-1").strip()

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

        user_message = ("## Seed Prompt\n%s\n\n## Chat Messages\n%s") % (
            seed_prompt or "",
            messages_text,
        )

        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        _logger.info(
            "task_desc: calling GLM arn=%s region=%s prompt_len=%d",
            inference_arn,
            region,
            len(user_message),
        )
        import time as _time

        t0 = _time.monotonic()
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
        elapsed = _time.monotonic() - t0
        _logger.info(
            "task_desc: GLM response elapsed=%.2fs in_tokens=%d out_tokens=%d "
            "response_len=%d raw_output=%.500s",
            elapsed,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            len(response_text),
            response_text,
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


def _wrap_trajectory_message(
    msg, is_accepted=0, hints=None, is_auto_hint=False, auto_hint_iteration=0
):
    """Wrap an assistant or toolResult message with is_accepted/hints.

    User messages are returned as-is (no wrapper per client spec).
    """
    role = ""
    inner = msg.get("message", {})
    if isinstance(inner, dict):
        role = inner.get("role", "")
    if role in ("assistant", "toolResult"):
        wrapped = {
            "is_accepted": is_accepted,
            "hints": hints,
            "message": msg,
        }
        if is_auto_hint:
            wrapped["is_auto_hint"] = True
            wrapped["auto_hint_iteration"] = auto_hint_iteration
        return wrapped
    return msg


def _wrap_messages_with_turn_feedback(messages, turns):
    """Apply is_accepted / hints wrappers using per-turn feedback data.

    ``turns`` is an iterable of KenseiTurn records (sorted by turn_number).
    A turn with ``hints`` populated (and ``prompt`` empty) is a correction turn.
    The hints text is applied to that turn's assistant responses.
    """
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
        turn_feedback.append(
            (
                user_text,
                is_accepted,
                hint,
                getattr(t, "is_auto_hint", False),
                getattr(t, "auto_hint_iteration", 0),
            )
        )

    wrapped = []
    current_accepted = 0
    current_hints = None
    current_is_auto_hint = False
    current_auto_hint_iteration = 0
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
                current_is_auto_hint = turn_feedback[turn_idx][3]
                current_auto_hint_iteration = turn_feedback[turn_idx][4]
                turn_idx += 1
            elif user_text:
                current_accepted = turn_feedback[turn_idx][1]
                current_hints = turn_feedback[turn_idx][2]
                current_is_auto_hint = turn_feedback[turn_idx][3]
                current_auto_hint_iteration = turn_feedback[turn_idx][4]
                turn_idx += 1

        wrapped.append(
            _wrap_trajectory_message(
                msg,
                current_accepted,
                current_hints,
                current_is_auto_hint,
                current_auto_hint_iteration,
            )
        )

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
  - model_name: claude-opus-4.7
    litellm_params:
      model: bedrock/converse/{bedrock_arn}
      aws_region_name: {aws_region}
      thinking: {{"type": "adaptive", "display": "summarized"}}
      input_cost_per_token: 0.000005
      output_cost_per_token: 0.000025

  - model_name: kimi-k2.5
    litellm_params:
      model: moonshot/kimi-k2.5
      api_key: os.environ/MOONSHOT_API_KEY
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: glm-5
    litellm_params:
      model: bedrock/converse/{glm_bedrock_arn}
      aws_region_name: {glm_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: quiet_sand
    litellm_params:
      model: openai/quiet_sand
      api_base: https://api.llama.com/v1alpha
      api_key: os.environ/LLAMA_API_KEY
      drop_params: true

litellm_settings:
  drop_params: true
  modify_params: true
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
    mod_path = get_module_path("kensei")
    if not mod_path:
        return None
    return os.path.join(mod_path, "sandbox_docker")


class Kensei(models.Model):
    _name = "kensei.kensei"
    _description = "Kensei"

    is_kensei_admin = fields.Boolean(
        compute="_compute_is_kensei_admin",
        search="_search_is_kensei_admin",
    )

    # True when current user is a Quality Lead or a Project Lead. Gates UI
    # affordances that should be available to both roles (e.g. Regenerate
    # Description on trajectory sessions). Kept separate from
    # `is_kensei_admin` (QL-only) so widening access later doesn't silently
    # broaden that existing flag.
    is_ql_or_pl = fields.Boolean(
        compute="_compute_is_ql_or_pl",
    )

    @api.depends_context("uid")
    def _compute_is_kensei_admin(self):
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        for rec in self:
            rec.is_kensei_admin = is_admin

    def _search_is_kensei_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    @api.depends_context("uid")
    def _compute_is_ql_or_pl(self):
        user = self.env.user
        allowed = (
            user.has_group("etp_user_roles.group_quality_lead")
            or user.has_group("etp_user_roles.group_project_lead")
        )
        for rec in self:
            rec.is_ql_or_pl = allowed

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one("kensei.domain", string="Parsona")
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one(
        "hr.employee",
        default=lambda self: self.env.user.employee_id,
    )
    user_id = fields.Many2one(related="employee_id.user_id")

    persona_id = fields.Many2one(
        "kensei.persona", string="Persona", required=True, ondelete="restrict"
    )
    heart_taxonomy = fields.Many2many("kensei.taxonomy", string="HEART Taxonomy")
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
            ("high_stakes_actions", "high_stakes_actions"),
            ("borderline_requests", "borderline_requests"),
            ("private_data_usage", "private_data_usage"),
            (
                "ambiguous_requests_or_confirmations",
                "ambiguous_requests_or_confirmations",
            ),
            ("third_party_instructions", "third_party_instructions"),
            ("context_sensitive_tasks", "context_sensitive_tasks"),
            ("jailbreaks_and_prompt_injections", "jailbreaks_and_prompt_injections"),
            ("N/A", "N/A"),
        ],
        string="Safety Critical",
    )
    system_prompt = fields.Text(string="System Prompt")
    seed_prompt = fields.Text(string="Seed Prompt")
    initial_prompt = fields.Text(string="Initial Prompt")
    agent_md = fields.Text(string="Agent MD")
    soul_md = fields.Text(string="Soul MD")
    memory_md = fields.Text(string="Memory MD")
    email = fields.Char(string="Email")
    password = fields.Char(string="Password")
    gog_auth = fields.Text(string="Google Auth")
    gog_auth_token = fields.Text(string="Google Auth Token")
    outlook_username = fields.Char(string="Outlook Username")
    outlook_password = fields.Char(string="Outlook Password")
    eventbrite_username = fields.Char(string="Eventbrite Username")
    eventbrite_password = fields.Char(string="Eventbrite Password")
    strava_username = fields.Char(string="Strava Username")
    strava_password = fields.Char(string="Strava Password")
    oura_username = fields.Char(string="Oura Username")
    oura_password = fields.Char(string="Oura Password")
    instagram_username = fields.Char(string="Instagram Username")
    instagram_password = fields.Char(string="Instagram Password")
    facebook_username = fields.Char(string="Facebook Username")
    facebook_password = fields.Char(string="Facebook Password")
    threads_username = fields.Char(string="Threads Username")
    threads_password = fields.Char(string="Threads Password")

    # Sandboxes
    sandbox_ids = fields.One2many("kensei.sandbox", "kensei_id", string="Sandboxes")
    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )

    # Computed convenience fields — one shortcut per model type
    claude_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="Claude Sandbox"
    )
    glm_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="GLM Sandbox"
    )
    oneP_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="1P Sandbox"
    )
    onePA_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="1PA Sandbox"
    )
    onePB_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="1PB Sandbox"
    )
    onePC_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="1PC Sandbox"
    )
    onePD_sandbox_id = fields.Many2one(
        "kensei.sandbox", compute="_compute_sandbox_ids", string="1PD Sandbox"
    )

    claude_status = fields.Selection(related="claude_sandbox_id.docker_status")
    glm_status = fields.Selection(related="glm_sandbox_id.docker_status")

    claude_session_status = fields.Selection(related="claude_sandbox_id.session_status")
    glm_session_status = fields.Selection(related="glm_sandbox_id.session_status")

    claude_trajectory = fields.Text(string="Claude 4.7 Trajectory")
    glm_trajectory = fields.Text(string="GLM 5 Trajectory")
    onePA_trajectory = fields.Text(string="1PA Trajectory")
    onePB_trajectory = fields.Text(string="1PB Trajectory")
    onePC_trajectory = fields.Text(string="1PC Trajectory")
    onePD_trajectory = fields.Text(string="1PD Trajectory")
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

    rubrics = fields.Text(string="Rubrics (JSON)")

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

    # Token usage totals (aggregated from JSONL on stop, survives turn deletion)
    claude_input_tokens = fields.Integer(string="Claude Input Tokens", default=0)
    claude_output_tokens = fields.Integer(string="Claude Output Tokens", default=0)
    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
    oneP_input_tokens = fields.Integer(string="1P Input Tokens", default=0)
    oneP_output_tokens = fields.Integer(string="1P Output Tokens", default=0)
    onePA_input_tokens = fields.Integer(string="1PA Input Tokens", default=0)
    onePA_output_tokens = fields.Integer(string="1PA Output Tokens", default=0)
    onePB_input_tokens = fields.Integer(string="1PB Input Tokens", default=0)
    onePB_output_tokens = fields.Integer(string="1PB Output Tokens", default=0)
    onePC_input_tokens = fields.Integer(string="1PC Input Tokens", default=0)
    onePC_output_tokens = fields.Integer(string="1PC Output Tokens", default=0)
    onePD_input_tokens = fields.Integer(string="1PD Input Tokens", default=0)
    onePD_output_tokens = fields.Integer(string="1PD Output Tokens", default=0)
    bedrock_input_tokens = fields.Integer(string="Bedrock QC Input Tokens", default=0)
    bedrock_output_tokens = fields.Integer(string="Bedrock QC Output Tokens", default=0)

    # Trajectory QC tokens (from trajectory_qc endpoint, Bedrock calls per-entry)
    traj_qc_input_tokens = fields.Integer(string="Traj QC Input Tokens", default=0)
    traj_qc_output_tokens = fields.Integer(string="Traj QC Output Tokens", default=0)
    # Task description generation tokens (trajectory-level + task-level)
    taskdesc_input_tokens = fields.Integer(string="Task Desc Input Tokens", default=0)
    taskdesc_output_tokens = fields.Integer(string="Task Desc Output Tokens", default=0)
    # Golden trajectory generation tokens
    golden_input_tokens = fields.Integer(string="Golden Gen Input Tokens", default=0)
    golden_output_tokens = fields.Integer(string="Golden Gen Output Tokens", default=0)
    # Qwen auto-hint evaluation tokens
    kimi_eval_input_tokens = fields.Integer(string="Qwen Eval Input Tokens", default=0)
    kimi_eval_output_tokens = fields.Integer(
        string="Qwen Eval Output Tokens", default=0
    )

    # Auto-process (RabbitMQ batch processing)
    auto_process_status = fields.Selection(
        [
            ("none", "None"),
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="none",
        string="Auto Process Status",
        index=True,
    )
    auto_process_error = fields.Text(string="Auto Process Error")

    @api.depends("sandbox_ids", "sandbox_ids.model_type")
    def _compute_sandbox_ids(self):
        for rec in self:
            for mtype, field in [
                ("claude", "claude_sandbox_id"),
                ("glm", "glm_sandbox_id"),
                ("1p", "oneP_sandbox_id"),
                ("1pa", "onePA_sandbox_id"),
                ("1pb", "onePB_sandbox_id"),
                ("1pc", "onePC_sandbox_id"),
                ("1pd", "onePD_sandbox_id"),
            ]:
                sandbox = rec.sandbox_ids.filtered(
                    lambda s, mt=mtype: s.model_type == mt
                )[:1]
                setattr(rec, field, sandbox.id if sandbox else False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.ensure_sandboxes()
        return records

    def ensure_sandboxes(self):
        from .kensei_sandbox import MODEL_TYPES
        for rec in self:
            existing = rec.sandbox_ids.mapped("model_type")
            for mtype, _label in MODEL_TYPES:
                if mtype not in existing:
                    self.env["kensei.sandbox"].create(
                        {"kensei_id": rec.id, "model_type": mtype}
                    )

    # ── Turns helper (aggregates across all sandboxes) ──────────

    def _get_all_turns(self):
        self.ensure_one()
        turns = self.env["kensei.turn"]
        for sandbox in self.sandbox_ids:
            turns |= sandbox.turn_ids
        return turns.sorted("turn_number")

    # ── Actions ─────────────────────────────────────────────────

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "kensei.turn",
            "view_mode": "list,form",
            "domain": [("sandbox_id", "in", self.sandbox_ids.ids)],
            "context": {"default_kensei_id": self.id},
        }

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/kensei/chat/export_session?task_id={self.id}",
            "target": "self",
        }

    def action_delete_trajectory_entry(self, field_name, entry_index):
        self.ensure_one()
        valid_fields = {
            "claude_trajectory",
            "glm_trajectory",
            "onePA_trajectory",
            "onePB_trajectory",
            "onePC_trajectory",
            "onePD_trajectory",
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

    def action_delete_trajectory_for_sandbox(self, sandbox_id):
        self.ensure_one()

        if not (
            self.env.user.has_group("etp_user_roles.group_quality_lead")
            or self.env.user.has_group("etp_user_roles.group_project_lead")
        ):
            raise AccessError(
                "Only Quality Leads and Project Leads can delete trajectories."
            )

        trajectory_field_map = {
            "claude": "claude_trajectory",
            "glm": "glm_trajectory",
            "1pa": "onePA_trajectory",
            "1pb": "onePB_trajectory",
            "1pc": "onePC_trajectory",
            "1pd": "onePD_trajectory",
        }

        token_fields_map = {
            "claude": ("claude_input_tokens", "claude_output_tokens"),
            "glm": ("glm_input_tokens", "glm_output_tokens"),
            "1pa": ("onePA_input_tokens", "onePA_output_tokens"),
            "1pb": ("onePB_input_tokens", "onePB_output_tokens"),
            "1pc": ("onePC_input_tokens", "onePC_output_tokens"),
            "1pd": ("onePD_input_tokens", "onePD_output_tokens"),
        }

        sandbox = self.env["kensei.sandbox"].browse(int(sandbox_id or 0))
        if not sandbox.exists() or sandbox.kensei_id.id != self.id:
            raise UserError("Sandbox not found for this task.")

        if sandbox.docker_status in ("starting", "running"):
            raise UserError(
                "Stop the sandbox before deleting its trajectory (current status: %s)."
                % sandbox.docker_status
            )

        field_name = trajectory_field_map.get(sandbox.model_type)
        if not field_name:
            raise UserError(
                "Unknown model type for sandbox: %s" % sandbox.model_type
            )

        turn_count = len(sandbox.turn_ids)
        sandbox.turn_ids.unlink()

        write_values: dict = {field_name: False}
        token_fields = token_fields_map.get(sandbox.model_type)
        if token_fields:
            write_values[token_fields[0]] = 0
            write_values[token_fields[1]] = 0
        self.write(write_values)

        _logger.info(
            "Deleted %s trajectory and %d turns for sandbox=%s task=%s by user=%s",
            field_name,
            turn_count,
            sandbox.id,
            self.id,
            self.env.user.login,
        )
        return True

    def action_delete_trajectory_by_field(self, field_name):
        self.ensure_one()

        field_to_model = {
            "claude_trajectory": "claude",
            "glm_trajectory": "glm",
            "onePA_trajectory": "1pa",
            "onePB_trajectory": "1pb",
            "onePC_trajectory": "1pc",
            "onePD_trajectory": "1pd",
        }

        if field_name == "golden_trajectory":
            if not (
                self.env.user.has_group("etp_user_roles.group_quality_lead")
                or self.env.user.has_group("etp_user_roles.group_project_lead")
            ):
                raise AccessError(
                    "Only Quality Leads and Project Leads can delete trajectories."
                )
            self.write({
                "golden_trajectory": False,
                "golden_input_tokens": 0,
                "golden_output_tokens": 0,
            })
            _logger.info(
                "Deleted golden_trajectory for task=%s by user=%s",
                self.id,
                self.env.user.login,
            )
            return True

        model_type = field_to_model.get(field_name)
        if not model_type:
            raise UserError("Unknown trajectory field: %s" % field_name)

        sandbox = self.env["kensei.sandbox"].search(
            [("kensei_id", "=", self.id), ("model_type", "=", model_type)],
            limit=1,
        )
        if not sandbox:
            raise UserError(
                "No %s sandbox exists for this task yet." % model_type
            )

        return self.action_delete_trajectory_for_sandbox(sandbox.id)

    def action_generate_golden_trajectory(self):
        self.ensure_one()
        if not self.claude_trajectory and not self.glm_trajectory:
            raise UserError(
                "At least one of Claude or Kimi trajectory is required. "
                "Stop the corresponding sandbox first to capture its trajectory."
            )
        if not self.persona_id:
            raise UserError("No persona selected.")

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
        has_any = self.claude_trajectory or self.glm_trajectory or self.oneP_trajectory
        if not has_any:
            raise UserError(
                "At least one model trajectory is required to generate a task description."
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

    # ── Trajectory export ───────────────────────────────────────

    def build_trajectory_json(self):
        self.ensure_one()
        model_name = ""
        all_turns = self._get_all_turns().sorted("turn_number")
        for t in reversed(all_turns):
            if t.model_name:
                model_name = t.model_name
                break

        meta_info = {
            "task_type": self.task_type or "",
            "task_description": self.task_id or "",
            "task_completion_status": "success",
            "system_prompt": self.system_prompt or "",
            "platform": "macOS",
            "persona": self.persona_id.name if self.persona_id else "",
            "model": model_name,
            "difficulty": self.difficulty or "",
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
            .get_param("kensei.deployment_mode", "local")
            .strip()
        )

    @api.model
    def _cron_reconcile_sandboxes(self):
        self.env["kensei.sandbox"]._cron_reconcile()

    # ── Auto-process (RabbitMQ batch processing) ──────────────────────

    def action_publish_auto_process(self):
        """Publish selected tasks to the RabbitMQ auto_process queue."""
        from ..services.rabbitmq_service import batch_publish_auto_process_tasks

        eligible = self.filtered(
            lambda t: (
                t.auto_process_status in ("none", "failed")
                and (t.initial_prompt or "").strip()
            )
        )
        if not eligible:
            return

        eligible.write({"auto_process_status": "queued", "auto_process_error": False})
        batch_publish_auto_process_tasks(eligible.ids)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Auto Process",
                "message": "%d task(s) queued for auto-processing." % len(eligible),
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def auto_process_claim_task(self, task_id):
        """Atomically claim a task for auto-processing. Called via XML-RPC."""
        task = self.browse(task_id)
        if not task.exists():
            return {"skip": True, "reason": "not_found"}

        if task.auto_process_status != "queued":
            return {"skip": True, "reason": "status_%s" % task.auto_process_status}

        # Atomic claim via SQL to prevent race conditions
        self.env.cr.execute(
            "UPDATE kensei_kensei SET auto_process_status = 'processing' "
            "WHERE id = %s AND auto_process_status = 'queued' RETURNING id",
            [task_id],
        )
        claimed = self.env.cr.fetchone()
        if not claimed:
            return {"skip": True, "reason": "already_claimed"}

        task.invalidate_recordset()

        # Find Claude sandbox
        claude_sandbox = self.env["kensei.sandbox"].search(
            [("kensei_id", "=", task_id), ("model_type", "=", "claude")], limit=1
        )
        if not claude_sandbox:
            return {"skip": True, "reason": "no_claude_sandbox"}

        # Check if sandbox already has turns
        if claude_sandbox.turn_ids:
            return {"skip": True, "reason": "has_turns"}

        return {
            "task_id": task_id,
            "sandbox_id": claude_sandbox.id,
            "docker_status": claude_sandbox.docker_status or "stopped",
            "initial_prompt": task.initial_prompt or "",
            "system_prompt": task.system_prompt or "",
        }

    @api.model
    def auto_process_mark_done(self, task_id, status="done", error=""):
        """Mark a task as done or failed after auto-processing."""
        task = self.browse(task_id)
        if not task.exists():
            return False
        vals = {"auto_process_status": status}
        if error:
            vals["auto_process_error"] = str(error)[:2000]
        task.write(vals)
        return True


class KenseiTurn(models.Model):
    _name = "kensei.turn"
    _description = "Kensei Turn"
    _order = "turn_number asc, id asc"

    sandbox_id = fields.Many2one(
        "kensei.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    kensei_id = fields.Many2one(related="sandbox_id.kensei_id", store=True, readonly=True)
    employee_id = fields.Many2one(
        related="kensei_id.employee_id", store=True, readonly=True
    )
    turn_number = fields.Integer(string="Turn Number")
    turn_status = fields.Selection(
        [
            ("Pending", "Pending"),
            ("Streaming", "Streaming"),
            ("Completed", "Completed"),
            ("TimedOut", "Timed Out"),
        ]
    )
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
    claude_input_tokens = fields.Integer(string="Claude Input Tokens", default=0)
    claude_output_tokens = fields.Integer(string="Claude Output Tokens", default=0)
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
    is_auto_hint = fields.Boolean(
        string="Is Auto Hint",
        default=False,
        help="True if this turn was generated by automated GLM QC, not a human.",
    )
    auto_hint_iteration = fields.Integer(
        string="Auto Hint Iteration",
        default=0,
        help="Which iteration of the auto-hint loop produced this turn (1-5). 0 = not auto-hint.",
    )
    auto_hint_group_id = fields.Char(
        string="Auto Hint Group ID",
        help="UUID linking all turns in a single auto-hint evaluation loop.",
    )
    attachments = fields.Text(
        string="Attachments (JSON)",
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


class KenseiTaxonomy(models.Model):
    _name = "kensei.taxonomy"
    _description = "Kensei Taxonomy"

    name = fields.Char(string="Name", required=True, unique=True)
