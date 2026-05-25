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

_GOLDEN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kensei2-golden")
_GOLDEN_GENERATING = set()
_GOLDEN_LOCK = threading.Lock()

_TASKDESC_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kensei2-taskdesc")
_TASKDESC_GENERATING = set()
_TASKDESC_LOCK = threading.Lock()

_TESTWEIGHT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kensei2-testweight")
_TESTWEIGHT_GENERATING = set()
_TESTWEIGHT_LOCK = threading.Lock()

_golden_prompt_cache = None
_taskdesc_prompt_cache = None
_testweight_prompt_cache = None


def _get_golden_prompt():
    global _golden_prompt_cache
    if _golden_prompt_cache is not None:
        return _golden_prompt_cache
    mod_path = get_module_path("kensei2")
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
    mod_path = get_module_path("kensei2")
    if not mod_path:
        return ""
    path = os.path.join(mod_path, "task_description_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _taskdesc_prompt_cache = f.read().strip()
    else:
        _taskdesc_prompt_cache = ""
    return _taskdesc_prompt_cache


def _get_test_weight_prompt():
    global _testweight_prompt_cache
    if _testweight_prompt_cache is not None:
        return _testweight_prompt_cache
    mod_path = get_module_path("kensei2")
    if not mod_path:
        return ""
    path = os.path.join(mod_path, "test_weight_assignment_system_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _testweight_prompt_cache = f.read().strip()
    else:
        _testweight_prompt_cache = ""
    return _testweight_prompt_cache


def _run_golden_generation_background(db_name, task_id, notify_partner_id):
    try:
        # Phase 1: read all inputs
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
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
            inference_arn = (ICP.get_param("kensei2.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("kensei2.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = (dotenv.get("KENSEI2_AWS_BEARER_TOKEN") or dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "")).strip()

        if not api_key:
            raise RuntimeError("KENSEI2_AWS_BEARER_TOKEN (or AWS_BEARER_TOKEN_BEDROCK) not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Kensei2"
            )

        system_prompt = _get_golden_prompt()

        delivery_schema = ""
        schema_path = os.path.join(
            get_module_path("kensei2") or "", "Delivery_Schema.json"
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
                    task = env["kensei2.kensei2"].browse(task_id)
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
                            "kensei2/golden_ready",
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
                task = env["kensei2.kensei2"].browse(task_id)
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
                            "kensei2/golden_ready",
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
            task = env["kensei2.kensei2"].browse(task_id)
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
            inference_arn = (ICP.get_param("kensei2.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("kensei2.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = (dotenv.get("KENSEI2_AWS_BEARER_TOKEN") or dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "")).strip()

        if not api_key:
            raise RuntimeError("KENSEI2_AWS_BEARER_TOKEN (or AWS_BEARER_TOKEN_BEDROCK) not set in .env")
        if not inference_arn:
            raise RuntimeError(
                "Bedrock Inference ARN not configured in Settings > Kensei2"
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
                    task = env["kensei2.kensei2"].browse(task_id)
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
                            "kensei2/taskdesc_ready",
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
                task = env["kensei2.kensei2"].browse(task_id)
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
                            "kensei2/taskdesc_ready",
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


def _run_test_weight_generation_background(db_name, task_id, notify_partner_id):
    try:
        # Phase 1: read all inputs
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if not task.exists():
                _logger.error("Test weight gen: task %s does not exist", task_id)
                return

            initial_prompt = task.initial_prompt or ""
            test_code = task._get_best_test_code() or ""
            rubrics = task.rubrics or ""
            task_toml = task._build_harbor_task_toml() or ""

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("kensei2.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("kensei2.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = (dotenv.get("KENSEI2_AWS_BEARER_TOKEN") or dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "")).strip()

        if not api_key:
            raise RuntimeError("KENSEI2_AWS_BEARER_TOKEN (or AWS_BEARER_TOKEN_BEDROCK) not set in .env")
        if not inference_arn:
            raise RuntimeError("Bedrock Inference ARN not configured in Settings > Kensei2")
        if not test_code:
            raise RuntimeError("No test code available. Generate tests first.")

        system_prompt = _get_test_weight_prompt()
        if not system_prompt:
            raise RuntimeError("test_weight_assignment_system_prompt.md not found in kensei2 module")

        # Assemble user message with all available context
        parts = []
        parts.append("## instruction.md\n%s" % initial_prompt)
        parts.append("## test_outputs.py\n```python\n%s\n```" % test_code)
        if task_toml:
            parts.append("## task.toml\n```toml\n%s\n```" % task_toml)
        if rubrics:
            parts.append("## rubrics.json\n```json\n%s\n```" % rubrics)
        user_message = "\n\n".join(parts)

        # Phase 2: call LLM (no DB cursor held)
        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=4096,
            temperature=0.3,
            timeout=300.0,
        )
        _logger.info(
            "Test weights generated for task %s (%d chars, tokens: %s)",
            task_id,
            len(response_text),
            usage,
        )

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.index("\n")
            cleaned = cleaned[first_newline + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        json_match = _re.search(r'\[.*\]', cleaned, _re.DOTALL)
        if not json_match:
            raise ValueError("No JSON array found in LLM response. Raw: %s" % cleaned[:500])
        cleaned = json_match.group(0)

        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON array of test weight objects, got %s" % type(parsed).__name__)
        cleaned = json.dumps(parsed, indent=2, ensure_ascii=False)

        # Phase 3: write result and apply scores to test results
        weight_map = {w["test_name"]: w["weight"] for w in parsed if "test_name" in w}
        score_clamp_map = {}
        for tname, w in weight_map.items():
            if w >= 100:
                score_clamp_map[tname] = 30
            elif w <= -30:
                score_clamp_map[tname] = -30
            else:
                score_clamp_map[tname] = max(-30, min(30, w))

        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["kensei2.kensei2"].browse(task_id)
                    if not task.exists():
                        return
                    task.write({
                        "test_weights": cleaned,
                        "test_weights_status": "done",
                        "test_weights_error": False,
                    })

                    if score_clamp_map:
                        test_results = env["kensei2.test.result"].sudo().search([
                            ("kensei2_id", "=", task_id),
                        ])
                        scores_json = json.dumps(score_clamp_map, ensure_ascii=False)
                        for tr in test_results:
                            tr.write({"test_scores": scores_json})

                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "kensei2/test_weights_ready",
                            {
                                "task_id": task_id,
                                "status": "done",
                            },
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise

    except Exception as e:
        _logger.exception("Test weight generation failed for task %s", task_id)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                task = env["kensei2.kensei2"].browse(task_id)
                if task.exists():
                    task.write({
                        "test_weights_status": "error",
                        "test_weights_error": str(e)[:1000],
                    })
                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "kensei2/test_weights_ready",
                            {
                                "task_id": task_id,
                                "status": "error",
                                "error": str(e)[:500],
                            },
                        )
        except Exception:
            _logger.exception(
                "Failed to write test weight error status for task %s", task_id
            )
    finally:
        with _TESTWEIGHT_LOCK:
            _TESTWEIGHT_GENERATING.discard(task_id)


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


def generate_task_description_sync(env, seed_prompt, messages_json, system_prompt=None):
    """Call Qwen/Bedrock to generate a single-line task description.

    *system_prompt* overrides the kensei2-local prompt when provided —
    callers needing to reuse the talos task-description prompt pass that
    file's contents in directly.

    Returns:
        Tuple of (description_string, usage_dict).
    """
    try:
        ICP = env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("kensei2.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("kensei2.bedrock_region") or "ap-south-1").strip()

        dotenv = _load_dotenv()
        api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

        if not api_key or not inference_arn:
            _logger.warning("generate_task_description_sync: missing credentials")
            return "", {}

        if system_prompt is None:
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

    ``turns`` is an iterable of Kensei2Turn records (sorted by turn_number).
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


def _unwrap_trajectory_messages(messages):
    """Unwrap hint-wrapper format and assign sequential turn_index."""
    unwrapped = []
    for msg in messages:
        if (
            "message" in msg
            and isinstance(msg["message"], dict)
            and "message" in msg["message"]
        ):
            actual = msg["message"]
            unwrapped.append(actual)
        else:
            unwrapped.append(msg)
    for idx, m in enumerate(unwrapped):
        m["turn_index"] = idx
        m.pop("parentId", None)
    return unwrapped


def _load_dotenv():
    env = os.environ.copy()
    root = None
    try:
        conf_path = odoo_config.get('config') or getattr(odoo_config, 'rcfile', None)
    except Exception:
        conf_path = None
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

  - model_name: kimi-k2.6
    litellm_params:
      model: moonshot/kimi-k2.6
      api_key: os.environ/MOONSHOT_API_KEY
      temperature: 1.0
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

  - model_name: gpt-5.5
    litellm_params:
      model: openai/gpt-5.5
      api_key: os.environ/OPENAI_API_KEY
      reasoning_effort: high
      input_cost_per_token: 0.000005
      output_cost_per_token: 0.00003

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
    mod_path = get_module_path("kensei2")
    if not mod_path:
        return None
    return os.path.join(mod_path, "sandbox_docker")


class Kensei2(models.Model):
    _name = "kensei2.kensei2"
    _description = "Kensei2"

    is_kensei2_admin = fields.Boolean(
        compute="_compute_is_kensei2_admin",
        search="_search_is_kensei2_admin",
    )

    # True when current user is a Quality Lead or a Project Lead. Gates UI
    # affordances that should be available to both roles (e.g. Regenerate
    # Description on trajectory sessions). Kept separate from
    # `is_kensei2_admin` (QL-only) so widening access later doesn't silently
    # broaden that existing flag.
    is_ql_or_pl = fields.Boolean(
        compute="_compute_is_ql_or_pl",
    )

    @api.depends_context("uid")
    def _compute_is_kensei2_admin(self):
        is_admin = self.env.user.has_group("kensei2.group_kensei2_ql")
        for rec in self:
            rec.is_kensei2_admin = is_admin

    def _search_is_kensei2_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("kensei2.group_kensei2_ql")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    @api.depends_context("uid")
    def _compute_is_ql_or_pl(self):
        user = self.env.user
        allowed = (
            user.has_group("kensei2.group_kensei2_ql")
            or user.has_group("kensei2.group_kensei2_pl")
        )
        for rec in self:
            rec.is_ql_or_pl = allowed

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one("kensei2.domain", string="Parsona")
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one(
        "hr.employee",
        default=lambda self: self.env.user.employee_id,
    )
    employee_ids = fields.Many2many(
        "hr.employee",
        "kensei2_task_employee_rel",
        "task_id",
        "employee_id",
        string="Assigned Members",
        default=lambda self: self.env.user.employee_id,
    )
    user_id = fields.Many2one(related="employee_id.user_id")

    @api.onchange("employee_ids")
    def _onchange_employee_ids(self):
        """Keep employee_id in sync as the first member for backward compat."""
        for rec in self:
            if rec.employee_ids:
                rec.employee_id = rec.employee_ids[0]
            else:
                rec.employee_id = False

    @api.onchange("employee_id")
    def _onchange_employee_id(self):
        """Add employee_id to employee_ids if not already present."""
        for rec in self:
            if rec.employee_id and rec.employee_id not in rec.employee_ids:
                rec.employee_ids = [(4, rec.employee_id.id)]

    persona_id = fields.Many2one(
        "kensei2.persona", string="Persona", required=True, ondelete="restrict"
    )
    heart_taxonomy = fields.Many2many("kensei2.taxonomy", string="HEART Taxonomy")
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
    l1_classification = fields.Many2one('kensei2.domain', string="L1",
                                        domain="[('parent_id', '=', False)]")
    l2_classification = fields.Many2one('kensei2.domain', string="L2")
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
    sandbox_ids = fields.One2many("kensei2.sandbox", "kensei2_id", string="Sandboxes")

    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )

    # Computed convenience fields — one shortcut per model type
    claude_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="Claude Sandbox"
    )
    gpt_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="GPT Sandbox"
    )
    oneP_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="1P Sandbox"
    )
    onePA_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="1PA Sandbox"
    )
    onePB_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="1PB Sandbox"
    )
    onePC_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="1PC Sandbox"
    )
    onePD_sandbox_id = fields.Many2one(
        "kensei2.sandbox", compute="_compute_sandbox_ids", string="1PD Sandbox"
    )

    claude_status = fields.Selection(related="claude_sandbox_id.docker_status")
    gpt_status = fields.Selection(related="gpt_sandbox_id.docker_status")

    claude_session_status = fields.Selection(related="claude_sandbox_id.session_status")
    gpt_session_status = fields.Selection(related="gpt_sandbox_id.session_status")

    claude_trajectory = fields.Text(string="Claude 4.7 Trajectory")
    gpt_trajectory = fields.Text(string="GPT-5.5 Trajectory")
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

    test_code = fields.Text(string="Task Test Code (Generated)")
    test_code_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Test Code Status",
        default="idle",
    )
    test_code_error = fields.Text(string="Test Code Error")

    test_weights = fields.Text(string="Test Weights (JSON)")
    test_weights_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("generating", "Generating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Test Weights Status",
        default="idle",
    )
    test_weights_error = fields.Text(string="Test Weights Error")

    # Token usage totals (aggregated from JSONL on stop, survives turn deletion)
    claude_input_tokens = fields.Integer(string="Claude Input Tokens", default=0)
    claude_output_tokens = fields.Integer(string="Claude Output Tokens", default=0)
    gpt_input_tokens = fields.Integer(string="GPT Input Tokens", default=0)
    gpt_output_tokens = fields.Integer(string="GPT Output Tokens", default=0)
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

    # Batch execution fields
    batch_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("starting", "Starting"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("stopping", "Stopping"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        default="idle",
        string="Batch Status",
        index=True,
    )
    batch_prompt = fields.Text(string="Batch Prompt")
    batch_size = fields.Integer(string="Pods Per Model", default=8)
    batch_error = fields.Text(string="Batch Error")
    batch_started_at = fields.Datetime(string="Batch Started At")
    batch_completed_at = fields.Datetime(string="Batch Completed At")

    @api.depends("sandbox_ids", "sandbox_ids.model_type")
    def _compute_sandbox_ids(self):
        for rec in self:
            for mtype, field in [
                ("claude", "claude_sandbox_id"),
                ("gpt", "gpt_sandbox_id"),
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
        for vals in vals_list:
            if vals.get("employee_id") and not vals.get("employee_ids"):
                vals["employee_ids"] = [(4, vals["employee_id"])]
        records = super().create(vals_list)
        for rec in records:
            rec.ensure_batch_sandboxes()
        return records

    def ensure_batch_sandboxes(self):
        from .kensei2_sandbox import MODEL_TYPES

        SandboxModel = self.env["kensei2.sandbox"]
        for rec in self:
            batch_size = int(self.env["ir.config_parameter"].sudo().get_param(
                "kensei2.batch_size", rec.batch_size or 8
            ))
            existing = {
                (s.model_type, s.variant_index) for s in rec.sandbox_ids
            }
            vals_list = []
            for mtype, _label in MODEL_TYPES:
                for idx in range(1, batch_size + 1):
                    if (mtype, idx) not in existing:
                        vals_list.append({
                            "kensei2_id": rec.id,
                            "model_type": mtype,
                            "variant_index": idx,
                        })
            if vals_list:
                SandboxModel.create(vals_list)

    # ── Turns helper (aggregates across all sandboxes) ──────────

    def _get_all_turns(self):
        self.ensure_one()
        turns = self.env["kensei2.turn"]
        for sandbox in self.sandbox_ids:
            turns |= sandbox.turn_ids
        return turns.sorted("turn_number")

    # ── Actions ─────────────────────────────────────────────────

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "kensei2.turn",
            "view_mode": "list,form",
            "domain": [("sandbox_id", "in", self.sandbox_ids.ids)],
            "context": {"default_kensei2_id": self.id},
        }

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/kensei2/chat/export_session?task_id={self.id}",
            "target": "self",
        }

    def action_delete_trajectory_entry(self, field_name, entry_index):
        self.ensure_one()
        valid_fields = {
            "claude_trajectory",
            "glm_trajectory",
            "gpt_trajectory",
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

    # ── Batch execution ─────────────────────────────────────────

    def upload_batch_attachment(self, name, data_b64, mimetype):
        self.ensure_one()
        attachment = self.env["ir.attachment"].create({
            "name": name,
            "type": "binary",
            "datas": data_b64,
            "mimetype": mimetype or "application/octet-stream",
            "res_model": self._name,
            "res_id": self.id,
        })
        return {"attachment_id": attachment.id}

    def action_start_batch(self):
        """Deploy all sandbox pods without sending any prompt.

        After all pods are up, *batch_status* transitions to ``ready``.
        The prompt is sent later via :meth:`action_send_batch_prompt`.
        """
        self.ensure_one()
        if not self.persona_id:
            raise UserError(
                "No persona selected. Please select a persona and save "
                "before starting a batch."
            )
        if self.batch_status in ("starting", "ready", "running", "stopping"):
            raise UserError(
                "A batch is already %s. Wait for it to finish or stop it first."
                % self.batch_status
            )

        running = self.sandbox_ids.filtered(
            lambda s: s.docker_status in ("starting", "running")
        )
        if running:
            raise UserError(
                "%d sandbox(es) are still active. Stop them before "
                "starting a new batch." % len(running)
            )

        self.ensure_batch_sandboxes()

        SandboxModel = self.env["kensei2.sandbox"]
        mode = SandboxModel._deployment_mode()

        if mode != "k8s":
            from .kensei2_sandbox import _docker_available, _compose_cmd
            if not _docker_available():
                raise UserError(
                    "Docker is not available. Ensure the Docker daemon is running."
                )
            if not _compose_cmd():
                raise UserError("docker compose (or docker-compose) not found.")

        sandbox_ids = self.sandbox_ids.ids
        for sandbox in self.sandbox_ids:
            gateway_token = secrets.token_hex(32)
            write_vals = {
                "docker_status": "starting",
                "docker_error": False,
                "docker_gateway_token": gateway_token,
                "session_status": "not_started",
                "auto_hint_status": "idle",
                "auto_hint_iteration": 0,
                "auto_hint_group_id": False,
            }
            if mode != "k8s":
                gw_port, llm_port, db_port = sandbox._allocate_ports()
                write_vals["docker_port"] = gw_port
                write_vals["docker_litellm_port"] = llm_port
            sandbox.write(write_vals)

        self.write({
            "batch_status": "starting",
            "batch_prompt": False,
            "batch_error": False,
            "batch_started_at": fields.Datetime.now(),
            "batch_completed_at": False,
        })

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        from .kensei2_sandbox import _BATCH_POOL, _run_batch_deploy_background

        @self.env.cr.postcommit.add
        def _queue_batch():
            _BATCH_POOL.submit(
                _run_batch_deploy_background,
                db_name,
                task_id,
                sandbox_ids,
                mode,
                notify_partner_id,
            )

        _logger.info(
            "Batch deploy started: task=%s sandboxes=%d mode=%s",
            self.id, len(sandbox_ids), mode,
        )
        return True

    def action_send_batch_prompt(self, prompt, attachment_ids=None):
        """Send a prompt to all deployed (ready) sandbox pods.

        Only valid when *batch_status* is ``ready`` (all pods are up).
        Transitions *batch_status* to ``running``.
        """
        self.ensure_one()
        prompt = (prompt or "").strip()
        attachment_ids = attachment_ids or []
        if not prompt and not attachment_ids:
            raise UserError("A prompt or attachments are required.")
        if self.batch_status != "ready":
            raise UserError(
                "Batch is not ready for a prompt (current status: %s). "
                "Start the batch first and wait until all pods are up."
                % self.batch_status
            )

        sandbox_ids = self.sandbox_ids.filtered(
            lambda s: s.docker_status == "running"
        ).ids
        if not sandbox_ids:
            raise UserError("No running sandboxes to send the prompt to.")

        SandboxModel = self.env["kensei2.sandbox"]
        mode = SandboxModel._deployment_mode()

        self.write({
            "batch_status": "running",
            "batch_prompt": prompt,
        })

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id
        att_ids = list(attachment_ids) if attachment_ids else []

        from .kensei2_sandbox import _BATCH_POOL, _run_batch_prompt_background

        @self.env.cr.postcommit.add
        def _queue_prompt():
            _BATCH_POOL.submit(
                _run_batch_prompt_background,
                db_name,
                task_id,
                sandbox_ids,
                prompt,
                mode,
                notify_partner_id,
                att_ids,
            )

        _logger.info(
            "Batch prompt sent: task=%s sandboxes=%d prompt_len=%d attachments=%d",
            self.id, len(sandbox_ids), len(prompt), len(att_ids),
        )
        return True

    def action_send_selective_prompt(self, prompt, sandbox_ids, attachment_ids=None):
        """Send a prompt to a user-selected subset of running pods.

        Unlike :meth:`action_send_batch_prompt`, this is permitted in any
        batch_status as long as the chosen pods are currently running. It
        does not flip *batch_status* or stop pods afterwards — it's intended
        for re-sending to surviving pods when one has died mid-batch.
        """
        self.ensure_one()
        prompt = (prompt or "").strip()
        attachment_ids = attachment_ids or []
        if not prompt and not attachment_ids:
            raise UserError("A prompt or attachments are required.")
        if not sandbox_ids:
            raise UserError("Select at least one pod to send the prompt to.")

        requested = set(int(sid) for sid in sandbox_ids)
        eligible = self.sandbox_ids.filtered(
            lambda s: s.id in requested and s.docker_status == "running"
        )
        if not eligible:
            raise UserError(
                "None of the selected pods are currently running."
            )

        skipped = requested - set(eligible.ids)
        eligible_ids = eligible.ids

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id
        att_ids = list(attachment_ids) if attachment_ids else []

        from .kensei2_sandbox import _BATCH_POOL, _run_selective_prompt_background

        @self.env.cr.postcommit.add
        def _queue_selective_prompt():
            _BATCH_POOL.submit(
                _run_selective_prompt_background,
                db_name,
                task_id,
                eligible_ids,
                prompt,
                notify_partner_id,
                att_ids,
            )

        _logger.info(
            "Selective prompt queued: task=%s sandboxes=%d skipped=%d prompt_len=%d attachments=%d",
            self.id, len(eligible_ids), len(skipped), len(prompt), len(att_ids),
        )
        return {"sent_to": eligible_ids, "skipped": sorted(skipped)}

    def action_stop_batch(self):
        self.ensure_one()
        if self.batch_status not in ("starting", "ready", "running"):
            raise UserError(
                "No active batch to stop (current status: %s)." % self.batch_status
            )

        non_stopped = self.sandbox_ids.filtered(
            lambda s: s.docker_status != "stopped"
        )
        if not non_stopped:
            self.write({
                "batch_status": "done",
                "batch_completed_at": fields.Datetime.now(),
            })
            return True

        self.write({"batch_status": "stopping"})

        sandbox_ids = non_stopped.ids
        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        from .kensei2_sandbox import _BATCH_POOL, _run_batch_stop_background

        @self.env.cr.postcommit.add
        def _queue_batch_stop():
            _BATCH_POOL.submit(
                _run_batch_stop_background,
                db_name,
                task_id,
                sandbox_ids,
                notify_partner_id,
            )

        _logger.info(
            "Batch manual stop: task=%s stopping %d sandboxes",
            self.id, len(non_stopped),
        )
        return True

    @api.model
    def _cron_check_batch(self):
        stuck_cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=30)
        stuck = self.search([
            ("batch_status", "in", ("starting", "ready", "running", "stopping")),
            ("batch_started_at", "<", stuck_cutoff),
        ])
        for task in stuck:
            active = task.sandbox_ids.filtered(
                lambda s: s.docker_status in ("starting", "running")
            )
            if not active:
                task.write({
                    "batch_status": "error",
                    "batch_error": "Batch appears stuck with no active sandboxes.",
                    "batch_completed_at": fields.Datetime.now(),
                })
                _logger.warning(
                    "Marked stuck batch as error: task=%s (no active sandboxes)",
                    task.id,
                )
            else:
                _logger.info(
                    "Batch still active: task=%s (%d sandboxes running)",
                    task.id, len(active),
                )

    def action_delete_trajectory_for_sandbox(self, sandbox_id):
        self.ensure_one()

        if not (
            self.env.user.has_group("kensei2.group_kensei2_ql")
            or self.env.user.has_group("kensei2.group_kensei2_pl")
        ):
            raise AccessError(
                "Only Quality Leads and Project Leads can delete trajectories."
            )

        trajectory_field_map = {
            "claude": "claude_trajectory",
            "glm": "glm_trajectory",
            "gpt": "gpt_trajectory",
            "1pa": "onePA_trajectory",
            "1pb": "onePB_trajectory",
            "1pc": "onePC_trajectory",
            "1pd": "onePD_trajectory",
        }

        token_fields_map = {
            "claude": ("claude_input_tokens", "claude_output_tokens"),
            "glm": ("glm_input_tokens", "glm_output_tokens"),
            "gpt": ("gpt_input_tokens", "gpt_output_tokens"),
            "1pa": ("onePA_input_tokens", "onePA_output_tokens"),
            "1pb": ("onePB_input_tokens", "onePB_output_tokens"),
            "1pc": ("onePC_input_tokens", "onePC_output_tokens"),
            "1pd": ("onePD_input_tokens", "onePD_output_tokens"),
        }

        sandbox = self.env["kensei2.sandbox"].browse(int(sandbox_id or 0))
        if not sandbox.exists() or sandbox.kensei2_id.id != self.id:
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
            "gpt_trajectory": "gpt",
            "onePA_trajectory": "1pa",
            "onePB_trajectory": "1pb",
            "onePC_trajectory": "1pc",
            "onePD_trajectory": "1pd",
        }

        if field_name == "golden_trajectory":
            if not (
                self.env.user.has_group("kensei2.group_kensei2_ql")
                or self.env.user.has_group("kensei2.group_kensei2_pl")
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

        sandbox = self.env["kensei2.sandbox"].search(
            [("kensei2_id", "=", self.id), ("model_type", "=", model_type)],
            limit=1,
        )
        if not sandbox:
            raise UserError(
                "No %s sandbox exists for this task yet." % model_type
            )

        return self.action_delete_trajectory_for_sandbox(sandbox.id)

    def action_generate_golden_trajectory(self):
        self.ensure_one()
        if not self.claude_trajectory and not self.glm_trajectory and not self.gpt_trajectory:
            raise UserError(
                "At least one of Claude, Kimi, or GPT trajectory is required. "
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
        has_any = self.claude_trajectory or self.glm_trajectory or self.gpt_trajectory or self.oneP_trajectory
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

    def action_generate_test_weights(self):
        self.ensure_one()
        test_code = self._get_best_test_code()
        if not test_code:
            raise UserError(
                "No test code available. Generate or run tests first."
            )
        if not self.initial_prompt:
            raise UserError("Task instruction (initial_prompt) is required.")

        with _TESTWEIGHT_LOCK:
            if self.id in _TESTWEIGHT_GENERATING:
                raise UserError("Test weight generation is already in progress.")
            _TESTWEIGHT_GENERATING.add(self.id)

        self.write(
            {"test_weights_status": "generating", "test_weights_error": False}
        )

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        @self.env.cr.postcommit.add
        def _queue_testweight():
            _TESTWEIGHT_POOL.submit(
                _run_test_weight_generation_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    # ── Trajectory export ───────────────────────────────────────

    def _slugify_task_type(self):
        self.ensure_one()
        l1 = self.l1_classification.name if self.l1_classification else ""
        l2 = self.l2_classification.name if self.l2_classification else ""
        if not l1 and not l2:
            return "uncategorized__uncategorized"
        slug_l1 = _re.sub(r"[^a-z0-9]+", "_", (l1 or "uncategorized").lower()).strip("_")
        slug_l2 = _re.sub(r"[^a-z0-9]+", "_", (l2 or "uncategorized").lower()).strip("_")
        return "%s__%s" % (slug_l1, slug_l2)

    def _build_multimodal_metadata(self):
        self.ensure_one()
        modality_tags = set()
        input_modalities = set()
        all_turns = self._get_all_turns().sorted("turn_number")
        for t in all_turns:
            if not t.attachments:
                continue
            try:
                atts = json.loads(t.attachments)
                if not isinstance(atts, list):
                    continue
                for att in atts:
                    mime = att.get("mimeType", "")
                    if mime:
                        input_modalities.add(mime)
                    if mime.startswith("image/"):
                        modality_tags.add("upload_image")
                    elif mime == "application/pdf":
                        modality_tags.add("pdf")
                    elif mime.startswith("video/"):
                        modality_tags.add("video")
                    elif mime.startswith("audio/"):
                        modality_tags.add("audio")
            except (json.JSONDecodeError, TypeError):
                continue

        output_modalities = ["text"]
        output_artifacts = self._build_output_artifacts()
        for art in output_artifacts:
            m = art.get("mime_type", "")
            if m.startswith("image/") and "image" not in output_modalities:
                output_modalities.append("image")
            elif m and not m.startswith("image/") and "file" not in output_modalities:
                output_modalities.append("file")

        return {
            "modality_tags": sorted(modality_tags),
            "taxonomy_l1": self.l1_classification.name if self.l1_classification else "",
            "taxonomy_l2": self.l2_classification.name if self.l2_classification else "",
            "media_necessity": "Multimodal input required for visual understanding task.",
            "cross_modal_reasoning": {
                "percentage": 50,
                "modalities_fused": ["text", "image"],
                "description": "Agent processes visual and text inputs together.",
            },
            "input_modalities": sorted(input_modalities),
            "output_modalities": output_modalities,
            "asset_realism_notes": "Natural user-uploaded content with realistic filenames and varying quality.",
        }

    def _build_input_files_manifest(self):
        self.ensure_one()
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        task_id = self.task_id or str(self.id)

        seen_filenames = set()
        manifest = []
        idx = 0
        all_turns = self._get_all_turns().sorted("turn_number")
        for t in all_turns:
            if not t.attachments:
                continue
            try:
                atts = json.loads(t.attachments)
                if not isinstance(atts, list):
                    continue
                for att in atts:
                    fname = att.get("name", "")
                    if not fname or fname in seen_filenames:
                        continue
                    seen_filenames.add(fname)
                    mime = att.get("mimeType", "")
                    stored_as = att.get("storedAs", "")
                    entry = {
                        "ref_id": "input_%d" % idx,
                        "filename": fname,
                        "mime_type": mime,
                        "role": "primary_reference",
                        "description": "User-uploaded %s file" % mime,
                        "size_bytes": att.get("size", 0),
                    }
                    if stored_as and bucket:
                        entry["source"] = "s3://%s/%s/input/tasks/%s/%s" % (
                            bucket, prefix, task_id, stored_as
                        )
                    manifest.append(entry)
                    idx += 1
            except (json.JSONDecodeError, TypeError):
                continue
        return manifest

    def _build_output_artifacts(self):
        self.ensure_one()
        import re as _re
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        task_id = self.task_id or str(self.id)

        media_ext_re = _re.compile(
            r"/home/node/\.openclaw/(?:workspace|uploads|media)/[^\s\"'`\n)]+\."
            r"(?:png|jpe?g|gif|webp|bmp|svg|heic|heif|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|csv|json|md|txt|html|docx?)",
            _re.IGNORECASE,
        )

        mime_map = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "svg": "image/svg+xml", "heic": "image/heic", "heif": "image/heif",
            "mp4": "video/mp4", "webm": "video/webm",
            "mov": "video/quicktime", "mp3": "audio/mpeg", "wav": "audio/wav",
            "ogg": "audio/ogg", "m4a": "audio/mp4", "pdf": "application/pdf",
            "csv": "text/csv", "json": "application/json", "md": "text/markdown",
            "txt": "text/plain", "html": "text/html",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        write_tool_names = {"write", "save_file", "create_file", "write_file"}
        workspace_prefix = "/home/node/.openclaw/"

        def _classify(ext):
            mime = mime_map.get(ext, "application/octet-stream")
            if mime.startswith("image"):
                return mime, "generated_image"
            if mime.startswith("video") or mime.startswith("audio"):
                return mime, "media"
            if mime == "application/pdf":
                return mime, "document"
            return mime, "data_export"

        def _add_artifact(path, seen, artifacts):
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename in seen:
                return
            seen.add(basename)
            ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
            mime, artifact_type = _classify(ext)
            entry = {
                "filename": basename,
                "mime_type": mime,
                "artifact_type": artifact_type,
                "description": "Agent-generated %s output" % ext.upper(),
                "container_path": path if path.startswith(workspace_prefix) else "",
            }
            if bucket:
                entry["source"] = "s3://%s/%s/output/tasks/%s/%s" % (
                    bucket, prefix, task_id, basename
                )
                entry["s3_url"] = "https://%s.s3.%s.amazonaws.com/%s/output/tasks/%s/%s" % (
                    bucket, region, prefix, task_id, basename
                )
            artifacts.append(entry)

        seen = set()
        artifacts = []
        all_turns = self._get_all_turns().sorted("turn_number")
        for t in all_turns:
            response_text = t.response or ""
            paths = media_ext_re.findall(response_text)
            for path in paths:
                _add_artifact(path, seen, artifacts)

            tc_raw = t.tool_calls or ""
            if tc_raw:
                try:
                    tc_list = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
                    if isinstance(tc_list, list):
                        for tc in tc_list:
                            if not isinstance(tc, dict):
                                continue
                            name = (tc.get("name") or "").lower()
                            if name in write_tool_names:
                                args = tc.get("args") or tc.get("arguments") or tc.get("input") or {}
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except (json.JSONDecodeError, TypeError):
                                        args = {}
                                fpath = args.get("path") or args.get("file_path") or args.get("filePath") or ""
                                if fpath and fpath.startswith(workspace_prefix):
                                    _add_artifact(fpath, seen, artifacts)
                            result = tc.get("result") or ""
                            if isinstance(result, str):
                                for rpath in media_ext_re.findall(result):
                                    _add_artifact(rpath, seen, artifacts)
                except (json.JSONDecodeError, TypeError):
                    pass

            traj_raw = t.trajectory_messages or ""
            if traj_raw:
                try:
                    traj_msgs = json.loads(traj_raw) if isinstance(traj_raw, str) else traj_raw
                    if isinstance(traj_msgs, list):
                        for msg in traj_msgs:
                            if not isinstance(msg, dict):
                                continue
                            inner = msg.get("message", msg)
                            if not isinstance(inner, dict):
                                continue
                            role = inner.get("role", "")
                            content = inner.get("content")
                            if role == "assistant" and isinstance(content, list):
                                for block in content:
                                    if not isinstance(block, dict):
                                        continue
                                    if block.get("type") in ("tool_use", "toolCall"):
                                        inp = block.get("input") or block.get("arguments") or {}
                                        if isinstance(inp, str):
                                            try:
                                                inp = json.loads(inp)
                                            except (json.JSONDecodeError, TypeError):
                                                inp = {}
                                        bname = (block.get("name") or "").lower()
                                        if bname in write_tool_names:
                                            fpath = inp.get("path") or inp.get("file_path") or inp.get("filePath") or ""
                                            if fpath and fpath.startswith(workspace_prefix):
                                                _add_artifact(fpath, seen, artifacts)
                            elif role == "tool" or role == "toolResult":
                                text = ""
                                if isinstance(content, str):
                                    text = content
                                elif isinstance(content, list):
                                    text = " ".join(
                                        b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                for rpath in media_ext_re.findall(text):
                                    _add_artifact(rpath, seen, artifacts)
                except (json.JSONDecodeError, TypeError):
                    pass

        return artifacts

    def action_download_harbor_bundle(self):
        return {
            "type": "ir.actions.act_url",
            "url": "/kensei2/harbor/download",
            "target": "self",
        }

    def action_mass_export_to_harbor(self):
        import threading
        from odoo.modules.module import get_module_path
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX
        from .kensei2_sandbox import _load_dotenv

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX

        if not bucket:
            raise UserError("S3 bucket not configured. Go to Settings → Kensei2.")

        skipped = []
        all_uploads = []

        module_path = get_module_path("kensei2")
        env_dir = os.path.join(module_path, "environment")
        env_files = []
        if os.path.isdir(env_dir):
            for dirpath, _dirnames, filenames in os.walk(env_dir):
                for fname in filenames:
                    if fname.startswith(".") or fname == "__pycache__":
                        continue
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, env_dir)
                    try:
                        with open(full_path, "rb") as f:
                            data = f.read()
                    except (IOError, OSError):
                        continue
                    content_type = "application/octet-stream"
                    if fname.endswith((".py", ".toml", ".txt", ".sh", ".csv", ".md")):
                        content_type = "text/plain"
                    elif fname.endswith(".json"):
                        content_type = "application/json"
                    elif fname.endswith((".yaml", ".yml")):
                        content_type = "text/yaml"
                    env_files.append({
                        "rel": rel_path,
                        "data": data,
                        "content_type": content_type,
                    })

        for rec in self:
            if not rec.initial_prompt:
                skipped.append(rec.task_id or str(rec.id))
                continue

            task_id = rec.task_id or str(rec.id)
            s3_task_prefix = "%s/harbor/%s" % (prefix, task_id)
            files = []

            files.append({
                "key": "prompt.txt",
                "data": (rec.initial_prompt or "").encode("utf-8"),
                "content_type": "text/plain",
            })

            rubric_export = "[]"
            if rec.rubrics:
                rubric_export = rec._transform_rubrics_for_export(rec.rubrics)
            files.append({
                "key": "rubric.json",
                "data": rubric_export.encode("utf-8"),
                "content_type": "application/json",
            })

            golden_parsed = {}
            golden_data = getattr(rec, "golden_trajectory", None)
            if golden_data:
                try:
                    golden_parsed = json.loads(golden_data)
                    if isinstance(golden_parsed, list) and golden_parsed:
                        golden_parsed = golden_parsed[0]
                except (json.JSONDecodeError, TypeError):
                    golden_parsed = {}
            files.append({
                "key": "golden_trajectory.json",
                "data": json.dumps(golden_parsed, indent=2, ensure_ascii=False).encode("utf-8"),
                "content_type": "application/json",
            })

            files.append({
                "key": "data/instruction.md",
                "data": (rec.initial_prompt or "").encode("utf-8"),
                "content_type": "text/markdown",
            })

            task_toml = rec._build_harbor_task_toml()
            files.append({
                "key": "data/task.toml",
                "data": task_toml.encode("utf-8"),
                "content_type": "text/plain",
            })

            for ef in env_files:
                files.append({
                    "key": "data/environment/%s" % ef["rel"],
                    "data": ef["data"],
                    "content_type": ef["content_type"],
                })

            files.append({
                "key": "data/environment/Dockerfile",
                "data": rec._generate_harbor_dockerfile(env_dir).encode("utf-8"),
                "content_type": "text/plain",
            })
            files.append({
                "key": "data/environment/docker-compose.yaml",
                "data": rec._generate_harbor_docker_compose(env_dir).encode("utf-8"),
                "content_type": "text/yaml",
            })

            files.append({
                "key": "data/tests/test.sh",
                "data": rec._generate_harbor_test_sh().encode("utf-8"),
                "content_type": "text/plain",
            })

            test_code = rec.test_code or rec._get_best_test_code() or ""
            files.append({
                "key": "data/tests/test_outputs.py",
                "data": test_code.encode("utf-8"),
                "content_type": "text/plain",
            })

            weights_data = rec.test_weights or "{}"
            files.append({
                "key": "data/tests/test_weights.json",
                "data": weights_data.encode("utf-8"),
                "content_type": "application/json",
            })

            files.append({
                "key": "data/solution/solve.sh",
                "data": rec._generate_harbor_solve_sh(env_dir).encode("utf-8"),
                "content_type": "text/plain",
            })

            trajectory_fields = {
                "claude": "claude_trajectory",
                "gpt": "gpt_trajectory",
            }
            model_pass_data = {}
            for traj_name, field_name in trajectory_fields.items():
                traj_data = getattr(rec, field_name, None)
                if not traj_data:
                    continue
                try:
                    sessions = json.loads(traj_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(sessions, list):
                    sessions = [sessions]
                for idx, session_entry in enumerate(sessions, start=1):
                    session_json = json.dumps(session_entry, indent=2, ensure_ascii=False)
                    files.append({
                        "key": "trajectories/%s/run_%d/output.json" % (traj_name, idx),
                        "data": session_json.encode("utf-8"),
                        "content_type": "application/json",
                    })

            test_results = self.env["kensei2.test.result"].sudo().search([
                ("kensei2_id", "=", rec.id),
                ("model_type", "!=", False),
                ("trajectory_index", ">", 0),
            ])
            for tr in test_results:
                run_idx = tr.trajectory_index
                model = tr.model_type
                verifier_prefix = "trajectories/%s/run_%d/task_output/logs/verifier" % (model, run_idx)

                reward = rec._compute_test_reward(tr)
                files.append({
                    "key": "%s/reward.txt" % verifier_prefix,
                    "data": str(reward).encode("utf-8"),
                    "content_type": "text/plain",
                })

                ctrf = rec._build_ctrf_from_test_result(tr)
                files.append({
                    "key": "%s/ctrf.json" % verifier_prefix,
                    "data": json.dumps(ctrf, indent=2, ensure_ascii=False).encode("utf-8"),
                    "content_type": "application/json",
                })

                model_pass_data.setdefault(model, []).append({
                    "run_index": run_idx,
                    "tests_total": tr.tests_total or 0,
                    "tests_passed": tr.tests_passed or 0,
                    "tests_failed": tr.tests_failed or 0,
                    "reward": reward,
                })

            for model_name, runs in model_pass_data.items():
                n = len(runs)
                avg_reward = round(sum(r["reward"] for r in runs) / n, 4) if n else 0.0
                pass_summary = {
                    "model": model_name,
                    "runs": n,
                    "average_reward": avg_reward,
                    "per_run": runs,
                }
                files.append({
                    "key": "trajectories/%s/pass_summary.json" % model_name,
                    "data": json.dumps(pass_summary, indent=2, ensure_ascii=False).encode("utf-8"),
                    "content_type": "application/json",
                })

            all_uploads.append((s3_task_prefix, files))

        if not all_uploads:
            raise UserError(
                "No exportable tasks. All selected tasks are missing Initial Prompt."
            )

        dotenv = _load_dotenv()
        access_key = (
            dotenv.get("KENSEI_S3_ACCESS_KEY_ID", "")
            or os.environ.get("KENSEI_S3_ACCESS_KEY_ID", "")
        )
        secret_key = (
            dotenv.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
            or os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
        )

        def _upload_batch(bkt, rgn, uploads, ak, sk):
            try:
                import boto3
                from botocore.config import Config as BotoConfig
                client_kwargs = {
                    "region_name": rgn,
                    "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
                }
                if ak and sk:
                    client_kwargs["aws_access_key_id"] = ak
                    client_kwargs["aws_secret_access_key"] = sk
                s3 = boto3.client("s3", **client_kwargs)
                total = 0
                for pfx, files in uploads:
                    for fm in files:
                        key = "%s/%s" % (pfx, fm["key"])
                        s3.put_object(
                            Bucket=bkt,
                            Key=key,
                            Body=fm["data"],
                            ContentType=fm["content_type"],
                        )
                    total += 1
                _logger.info("Harbor mass export: %d tasks uploaded to s3://%s/", total, bkt)
            except Exception:
                _logger.exception("Harbor mass export failed")

        thread = threading.Thread(
            target=_upload_batch,
            args=(bucket, region, all_uploads, access_key, secret_key),
            daemon=True,
        )
        thread.start()

        exported_recs = self.filtered(lambda r: r.initial_prompt)
        exported_recs.write({"task_status": "Submitted"})

        msg = "Exporting %d task(s) to S3." % len(all_uploads)
        if skipped:
            msg += " Skipped %d (no instruction): %s" % (len(skipped), ", ".join(skipped[:5]))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Mass Export Started",
                "message": msg,
                "type": "info",
                "sticky": False,
            },
        }

    def action_export_to_harbor(self):
        self.ensure_one()
        import threading
        from odoo.modules.module import get_module_path
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX
        from .kensei2_sandbox import _load_dotenv

        if not self.initial_prompt:
            raise UserError("Cannot export: Initial Prompt (instruction) is empty.")

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX

        if not bucket:
            raise UserError("S3 bucket not configured. Go to Settings → Kensei2.")

        task_id = self.task_id or str(self.id)
        s3_task_prefix = "%s/harbor/%s" % (prefix, task_id)

        files_to_upload = []

        files_to_upload.append({
            "key": "prompt.txt",
            "data": (self.initial_prompt or "").encode("utf-8"),
            "content_type": "text/plain",
        })

        rubric_export = "[]"
        if self.rubrics:
            rubric_export = self._transform_rubrics_for_export(self.rubrics)
        files_to_upload.append({
            "key": "rubric.json",
            "data": rubric_export.encode("utf-8"),
            "content_type": "application/json",
        })

        golden_parsed = {}
        golden_data = getattr(self, "golden_trajectory", None)
        if golden_data:
            try:
                golden_parsed = json.loads(golden_data)
                if isinstance(golden_parsed, list) and golden_parsed:
                    golden_parsed = golden_parsed[0]
            except (json.JSONDecodeError, TypeError):
                golden_parsed = {}
        files_to_upload.append({
            "key": "golden_trajectory.json",
            "data": json.dumps(golden_parsed, indent=2, ensure_ascii=False).encode("utf-8"),
            "content_type": "application/json",
        })

        files_to_upload.append({
            "key": "data/instruction.md",
            "data": (self.initial_prompt or "").encode("utf-8"),
            "content_type": "text/markdown",
        })

        task_toml = self._build_harbor_task_toml()
        files_to_upload.append({
            "key": "data/task.toml",
            "data": task_toml.encode("utf-8"),
            "content_type": "text/plain",
        })

        module_path = get_module_path("kensei2")
        env_dir = os.path.join(module_path, "environment")
        if os.path.isdir(env_dir):
            for dirpath, _dirnames, filenames in os.walk(env_dir):
                for fname in filenames:
                    if fname.startswith(".") or fname == "__pycache__":
                        continue
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, env_dir)
                    try:
                        with open(full_path, "rb") as f:
                            data = f.read()
                    except (IOError, OSError):
                        continue
                    content_type = "application/octet-stream"
                    if fname.endswith((".py", ".toml", ".txt", ".sh", ".csv", ".md")):
                        content_type = "text/plain"
                    elif fname.endswith(".json"):
                        content_type = "application/json"
                    elif fname.endswith(".yaml") or fname.endswith(".yml"):
                        content_type = "text/yaml"
                    files_to_upload.append({
                        "key": "data/environment/%s" % rel_path,
                        "data": data,
                        "content_type": content_type,
                    })

        files_to_upload.append({
            "key": "data/environment/Dockerfile",
            "data": self._generate_harbor_dockerfile(env_dir).encode("utf-8"),
            "content_type": "text/plain",
        })
        files_to_upload.append({
            "key": "data/environment/docker-compose.yaml",
            "data": self._generate_harbor_docker_compose(env_dir).encode("utf-8"),
            "content_type": "text/yaml",
        })

        files_to_upload.append({
            "key": "data/tests/test.sh",
            "data": self._generate_harbor_test_sh().encode("utf-8"),
            "content_type": "text/plain",
        })

        test_code = self.test_code or self._get_best_test_code() or ""
        files_to_upload.append({
            "key": "data/tests/test_outputs.py",
            "data": test_code.encode("utf-8"),
            "content_type": "text/plain",
        })

        weights_data = self.test_weights or "{}"
        files_to_upload.append({
            "key": "data/tests/test_weights.json",
            "data": weights_data.encode("utf-8"),
            "content_type": "application/json",
        })

        files_to_upload.append({
            "key": "data/solution/solve.sh",
            "data": self._generate_harbor_solve_sh(env_dir).encode("utf-8"),
            "content_type": "text/plain",
        })

        trajectory_fields = {
            "claude": "claude_trajectory",
            "gpt": "gpt_trajectory",
        }
        model_pass_data = {}

        for traj_name, field_name in trajectory_fields.items():
            traj_data = getattr(self, field_name, None)
            if not traj_data:
                continue
            try:
                sessions = json.loads(traj_data)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(sessions, list):
                sessions = [sessions]
            for idx, session_entry in enumerate(sessions, start=1):
                session_json = json.dumps(session_entry, indent=2, ensure_ascii=False)
                files_to_upload.append({
                    "key": "trajectories/%s/run_%d/output.json" % (traj_name, idx),
                    "data": session_json.encode("utf-8"),
                    "content_type": "application/json",
                })

        test_results = self.env["kensei2.test.result"].sudo().search([
            ("kensei2_id", "=", self.id),
            ("model_type", "!=", False),
            ("trajectory_index", ">", 0),
        ])
        for tr in test_results:
            run_idx = tr.trajectory_index
            model = tr.model_type
            verifier_prefix = "trajectories/%s/run_%d/task_output/logs/verifier" % (model, run_idx)

            reward = self._compute_test_reward(tr)
            files_to_upload.append({
                "key": "%s/reward.txt" % verifier_prefix,
                "data": str(reward).encode("utf-8"),
                "content_type": "text/plain",
            })

            ctrf = self._build_ctrf_from_test_result(tr)
            files_to_upload.append({
                "key": "%s/ctrf.json" % verifier_prefix,
                "data": json.dumps(ctrf, indent=2, ensure_ascii=False).encode("utf-8"),
                "content_type": "application/json",
            })

            model_pass_data.setdefault(model, []).append({
                "run_index": run_idx,
                "tests_total": tr.tests_total or 0,
                "tests_passed": tr.tests_passed or 0,
                "tests_failed": tr.tests_failed or 0,
                "reward": reward,
            })

        for model_name, runs in model_pass_data.items():
            n = len(runs)
            avg_reward = round(sum(r["reward"] for r in runs) / n, 4) if n else 0.0
            pass_summary = {
                "model": model_name,
                "runs": n,
                "average_reward": avg_reward,
                "per_run": runs,
            }
            files_to_upload.append({
                "key": "trajectories/%s/pass_summary.json" % model_name,
                "data": json.dumps(pass_summary, indent=2, ensure_ascii=False).encode("utf-8"),
                "content_type": "application/json",
            })

        dotenv = _load_dotenv()
        access_key = (
            dotenv.get("KENSEI_S3_ACCESS_KEY_ID", "")
            or os.environ.get("KENSEI_S3_ACCESS_KEY_ID", "")
        )
        secret_key = (
            dotenv.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
            or os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
        )

        def _upload(bkt, rgn, pfx, files, ak, sk):
            try:
                import boto3
                from botocore.config import Config as BotoConfig
                client_kwargs = {
                    "region_name": rgn,
                    "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
                }
                if ak and sk:
                    client_kwargs["aws_access_key_id"] = ak
                    client_kwargs["aws_secret_access_key"] = sk
                s3 = boto3.client("s3", **client_kwargs)
                for fm in files:
                    key = "%s/%s" % (pfx, fm["key"])
                    s3.put_object(
                        Bucket=bkt,
                        Key=key,
                        Body=fm["data"],
                        ContentType=fm["content_type"],
                    )
                _logger.info(
                    "Harbor export complete: s3://%s/%s/ (%d files)",
                    bkt, pfx, len(files),
                )
            except Exception:
                _logger.exception("Harbor export failed for task %s", pfx)

        thread = threading.Thread(
            target=_upload,
            args=(bucket, region, s3_task_prefix, files_to_upload, access_key, secret_key),
            daemon=True,
        )
        thread.start()

        self.write({"task_status": "Submitted"})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Export Started",
                "message": "Exporting task to s3://%s/%s/ (%d files)" % (
                    bucket, s3_task_prefix, len(files_to_upload)
                ),
                "type": "info",
                "sticky": False,
            },
        }

    def _build_harbor_task_toml(self):
        from odoo.modules.module import get_module_path

        lines = ['schema_version = "1.1"', ""]

        lines.append("[task]")
        task_name = self.task_id or "kensei2/%s" % self.id
        lines.append('name = "%s"' % task_name)
        if self.seed_prompt:
            desc = self.seed_prompt.replace('"', '\\"').replace("\n", " ")[:200]
            lines.append('description = "%s"' % desc)
        if self.employee_ids:
            authors = ", ".join(
                '{ name = "%s" }' % (emp.name or "").replace('"', '\\"')
                for emp in self.employee_ids
            )
            lines.append("authors = [%s]" % authors)
        keywords = []
        if self.task_type:
            keywords.append(self.task_type)
        if self.difficulty:
            keywords.append(self.difficulty)
        if keywords:
            lines.append("keywords = [%s]" % ", ".join('"%s"' % k for k in keywords))
        lines.append("")

        lines.append("[metadata]")
        if self.task_type:
            lines.append('category = "%s"' % self.task_type)
        if self.difficulty:
            lines.append('difficulty = "%s"' % self.difficulty)
        if self.trajectory_modifier:
            lines.append('trajectory_modifier = "%s"' % self.trajectory_modifier)
        if self.safety_critical and self.safety_critical != "N/A":
            lines.append('safety_critical = "%s"' % self.safety_critical)

        required_skills, distractor_skills = self._collect_harbor_skills()
        if required_skills:
            lines.append("required_skills = [%s]" % ", ".join('"%s"' % s for s in required_skills))
        if distractor_skills:
            lines.append("distractor_skills = [%s]" % ", ".join('"%s"' % s for s in distractor_skills))
        lines.append("")

        lines.append("[verifier]")
        lines.append("timeout_sec = 600.0")
        lines.append("")

        env_vars = self._collect_harbor_env_vars()

        if env_vars:
            lines.append("[verifier.env]")
            for k, v in env_vars.items():
                lines.append('%s = "%s"' % (k, v))
            lines.append("")

        lines.append("[agent]")
        lines.append("timeout_sec = 900.0")
        lines.append("")

        lines.append("[environment]")
        lines.append("build_timeout_sec = 600.0")
        lines.append("cpus = 1")
        lines.append("memory_mb = 4096")
        lines.append("storage_mb = 10240")
        lines.append("allow_internet = true")
        lines.append('skills_dir = "skills"')
        lines.append("")

        if env_vars:
            lines.append("[environment.env]")
            for k, v in env_vars.items():
                lines.append('%s = "%s"' % (k, v))
            lines.append("")

        lines.append("[environment.healthcheck]")
        lines.append('command = "curl -f http://localhost:8000/health"')
        lines.append("interval_sec = 5.0")
        lines.append("timeout_sec = 30.0")
        lines.append("retries = 3")
        lines.append("")

        if env_vars:
            lines.append("[solution.env]")
            for k, v in env_vars.items():
                lines.append('%s = "%s"' % (k, v))
            lines.append("")

        lines.append("[multimodal]")
        dep_tags = []
        if self.l1_classification:
            dep_tags.append(self.l1_classification)
        if self.l2_classification:
            dep_tags.append(self.l2_classification)
        lines.append("dependency_tags = [%s]" % ", ".join('"%s"' % t for t in dep_tags))
        lines.append("")

        lines.append("[evaluation]")
        lines.append("pass_at_k = 8")
        lines.append("")

        lines.append("[dimensions]")
        lines.append('complex = "medium"')
        lines.append('long_horizon = "false"')
        lines.append('objective = "true"')
        lines.append('multimodal = "%s"' % ("true" if dep_tags else "false"))
        lines.append('cross_modal_cross_api = "false"')
        lines.append('asset_complexity = "low"')
        lines.append("")

        return "\n".join(lines) + "\n"

    def _collect_harbor_skills(self):
        from odoo.modules.module import get_module_path

        required = []
        distractors = []
        module_path = get_module_path("kensei2")
        skills_dir = os.path.join(module_path, "environment", "skills")
        if not os.path.isdir(skills_dir):
            return required, distractors

        env_dir = os.path.join(module_path, "environment")
        service_names = set()
        for entry in os.listdir(env_dir):
            if os.path.isfile(os.path.join(env_dir, entry, "service.toml")):
                service_names.add(entry)

        for skill_name in sorted(os.listdir(skills_dir)):
            skill_path = os.path.join(skills_dir, skill_name)
            if not os.path.isdir(skill_path):
                continue
            base = skill_name.replace("-connector", "")
            if base in service_names:
                required.append(skill_name)
            else:
                distractors.append(skill_name)
        return required, distractors

    def _generate_harbor_dockerfile(self, env_dir):
        skills_dir = os.path.join(env_dir, "skills")
        skill_dirs = []
        if os.path.isdir(skills_dir):
            skill_dirs = [
                d for d in sorted(os.listdir(skills_dir))
                if os.path.isdir(os.path.join(skills_dir, d))
            ]

        lines = [
            "FROM ubuntu:24.04",
            "",
            "RUN apt-get update && apt-get install -y \\",
            "    curl jq python3 python3-pip \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
            "WORKDIR /app",
            "",
        ]
        if skill_dirs:
            agent_dirs = [
                "/root/.claude/skills",
                "/root/.codex/skills",
                "/root/.opencode/skills",
                "/root/.goose/skills",
                "/root/.factory/skills",
                "/root/.agents/skills",
                "/root/.gemini/skills",
                "/root/.cursor/skills",
            ]
            for ad in agent_dirs:
                lines.append("COPY skills %s" % ad)
        return "\n".join(lines) + "\n"

    def _generate_harbor_docker_compose(self, env_dir):
        services = []
        for entry in sorted(os.listdir(env_dir)):
            toml_path = os.path.join(env_dir, entry, "service.toml")
            if not os.path.isfile(toml_path):
                continue
            try:
                svc = self.env["kensei2.sandbox"]._parse_service_toml(toml_path)
            except Exception:
                continue
            services.append(svc)

        lines = ["services:"]

        lines.append("  main:")
        lines.append("    build:")
        lines.append("      context: .")
        lines.append("      dockerfile: Dockerfile")
        lines.append('    command: ["sh", "-c", "sleep infinity"]')
        if services:
            lines.append("    depends_on:")
            for svc in services:
                lines.append("      %s:" % svc["name"])
                lines.append("        condition: service_healthy")
        lines.append("    environment:")
        for svc in services:
            lines.append("      - %s=http://%s:%s" % (
                svc["env_var_name"], svc["name"], svc["port"]
            ))
        lines.append("    deploy:")
        lines.append("      resources:")
        lines.append("        limits:")
        lines.append("          cpus: ${CPUS:-1}")
        lines.append("          memory: ${MEMORY:-4096M}")
        lines.append("")

        for svc in services:
            lines.append("  %s:" % svc["name"])
            lines.append("    build:")
            lines.append("      context: ./%s" % svc["name"])
            lines.append("    expose:")
            lines.append('      - "%s"' % svc["port"])
            lines.append("    healthcheck:")
            lines.append(
                "      test: [\"CMD\", \"python3\", \"-c\", "
                "\"import urllib.request; urllib.request.urlopen('http://localhost:%s%s')\"]"
                % (svc["port"], svc.get("healthcheck_path", "/health"))
            )
            lines.append("      interval: 2s")
            lines.append("      timeout: 5s")
            lines.append("      retries: 15")
            lines.append("      start_period: 5s")
            lines.append("")

        return "\n".join(lines) + "\n"

    def _generate_harbor_test_sh(self):
        return (
            "#!/usr/bin/env bash\n"
            "\n"
            "apt-get update && apt-get install -y curl > /dev/null 2>&1\n"
            "curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh > /dev/null 2>&1\n"
            "source $HOME/.local/bin/env\n"
            "\n"
            "mkdir -p /logs/verifier\n"
            "\n"
            "uvx --with pytest==8.4.1 --with pytest-json-ctrf==0.3.5 --with requests pytest \\\n"
            "    --ctrf /logs/verifier/ctrf.json \\\n"
            "    tests/test_outputs.py -rA\n"
            "PYTEST_EXIT=$?\n"
            "\n"
            "python3 - <<'PYEOF' > /logs/verifier/reward.txt 2>/dev/null || echo 0 > /logs/verifier/reward.txt\n"
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "ctrf_path = Path('/logs/verifier/ctrf.json')\n"
            "weights_path = Path('tests/test_weights.json')\n"
            "\n"
            "if not ctrf_path.exists():\n"
            "    print(0.0)\n"
            "    raise SystemExit(0)\n"
            "\n"
            "ctrf = json.loads(ctrf_path.read_text())\n"
            "results = ctrf.get('results', {}) if isinstance(ctrf, dict) else {}\n"
            "tests = results.get('tests', []) if isinstance(results, dict) else []\n"
            "\n"
            "weights = {}\n"
            "if weights_path.exists():\n"
            "    try:\n"
            "        raw = json.loads(weights_path.read_text())\n"
            "        if isinstance(raw, dict):\n"
            "            weights = {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}\n"
            "        elif isinstance(raw, list):\n"
            "            for entry in raw:\n"
            "                if isinstance(entry, dict) and 'test_name' in entry:\n"
            "                    weights[entry['test_name']] = int(entry.get('weight', 0))\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "def weight_for(name):\n"
            "    bare = name.rsplit('::', 1)[-1]\n"
            "    return weights.get(bare, weights.get(name, 0))\n"
            "\n"
            "pos_total = 0\n"
            "pos_earned = 0\n"
            "neg_penalty = 0\n"
            "for t in tests:\n"
            "    if not isinstance(t, dict):\n"
            "        continue\n"
            "    name = t.get('name', '')\n"
            "    status = t.get('status', '')\n"
            "    w = weight_for(name)\n"
            "    if w > 0:\n"
            "        pos_total += w\n"
            "        if status == 'passed':\n"
            "            pos_earned += w\n"
            "    elif w < 0 and status == 'passed':\n"
            "        neg_penalty += abs(w)\n"
            "\n"
            "if pos_total <= 0:\n"
            "    total = sum(1 for t in tests if isinstance(t, dict))\n"
            "    passed = sum(1 for t in tests if isinstance(t, dict) and t.get('status') == 'passed')\n"
            "    print(round(passed / total, 4) if total else 0.0)\n"
            "else:\n"
            "    print(round(max(0.0, (pos_earned - neg_penalty) / pos_total), 4))\n"
            "PYEOF\n"
        )

    def _get_best_test_code(self):
        results = self.env["kensei2.test.result"].sudo().search([
            ("kensei2_id", "=", self.id),
            ("status", "=", "passed"),
            ("test_code", "!=", False),
        ], order="create_date desc", limit=1)
        if results:
            return results[0].test_code
        results = self.env["kensei2.test.result"].sudo().search([
            ("kensei2_id", "=", self.id),
            ("test_code", "!=", False),
        ], order="tests_passed desc, create_date desc", limit=1)
        return results[0].test_code if results else ""

    def _transform_rubrics_for_export(self, raw_json):
        """Transform internal rubric format to Harbor delivery format.

        Internal: [{label, claude[], glm[], gpt[], justification, score, type, evaluation_target, importance, is_positive}]
        Export:   [{criterion, is_positive, type, evaluation_target, importance, score, number}]
        """
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return raw_json
        if not isinstance(data, list):
            return raw_json
        export_rubrics = []
        for i, rubric in enumerate(data):
            export_rubrics.append({
                "criterion": rubric.get("label", ""),
                "is_positive": rubric.get("is_positive", True),
                "type": rubric.get("type", "task completion"),
                "evaluation_target": rubric.get("evaluation_target", "state change"),
                "importance": rubric.get("importance", "important"),
                "score": rubric.get("score", 1),
                "number": "R%d" % (i + 1),
            })
        return json.dumps(export_rubrics, ensure_ascii=False, indent=2)

    def _compute_test_reward(self, test_result):
        if not self.test_weights:
            total = test_result.tests_total or 0
            passed = test_result.tests_passed or 0
            return round(passed / total, 4) if total else 0.0

        try:
            raw = json.loads(self.test_weights)
            weights = {}
            if isinstance(raw, dict):
                weights = {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}
            elif isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict) and "test_name" in entry:
                        weights[entry["test_name"]] = int(entry.get("weight", 0))
        except (json.JSONDecodeError, TypeError):
            total = test_result.tests_total or 0
            passed = test_result.tests_passed or 0
            return round(passed / total, 4) if total else 0.0

        scores = {}
        if test_result.test_scores:
            try:
                scores = json.loads(test_result.test_scores)
            except (json.JSONDecodeError, TypeError):
                pass

        pos_total = 0
        pos_earned = 0
        neg_penalty = 0
        for test_name, w in weights.items():
            status = scores.get(test_name, "failed")
            if w > 0:
                pos_total += w
                if status == "passed":
                    pos_earned += w
            elif w < 0 and status == "passed":
                neg_penalty += abs(w)

        if pos_total <= 0:
            total = test_result.tests_total or 0
            passed = test_result.tests_passed or 0
            return round(passed / total, 4) if total else 0.0

        return round(max(0.0, (pos_earned - neg_penalty) / pos_total), 4)

    def _build_ctrf_from_test_result(self, test_result):
        tests = []
        if test_result.test_scores:
            try:
                scores = json.loads(test_result.test_scores)
                for test_name, status in scores.items():
                    tests.append({
                        "name": test_name,
                        "status": status if status in ("passed", "failed") else "failed",
                        "duration": 0,
                    })
            except (json.JSONDecodeError, TypeError):
                pass

        if not tests:
            for _ in range(test_result.tests_passed or 0):
                tests.append({"name": "test_unknown", "status": "passed", "duration": 0})
            for _ in range(test_result.tests_failed or 0):
                tests.append({"name": "test_unknown", "status": "failed", "duration": 0})

        return {
            "results": {
                "tool": {"name": "pytest", "version": "8.4.1"},
                "summary": {
                    "tests": test_result.tests_total or 0,
                    "passed": test_result.tests_passed or 0,
                    "failed": test_result.tests_failed or 0,
                    "pending": 0,
                    "skipped": 0,
                    "other": test_result.tests_errored or 0,
                },
                "tests": tests,
            },
        }

    def _generate_harbor_solve_sh(self, env_dir):
        env_vars = self._collect_harbor_env_vars()
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "python3 - <<'PY'",
            "import os, json, urllib.request",
            "",
        ]
        for env_key in sorted(env_vars.keys()):
            val = env_vars[env_key]
            lines.append(
                "%s = os.environ.get('%s', '%s').rstrip('/')"
                % (env_key.lower(), env_key, val)
            )
        lines.append("")
        lines.append(
            "print('Solution not yet implemented "
            "-- populate with API calls from golden trajectory.')"
        )
        lines.append("PY")
        lines.append("")
        return "\n".join(lines)

    def _collect_harbor_env_vars(self):
        from odoo.modules.module import get_module_path

        env_vars = {}
        module_path = get_module_path("kensei2")
        env_dir = os.path.join(module_path, "environment")
        if not os.path.isdir(env_dir):
            return env_vars

        for entry in sorted(os.listdir(env_dir)):
            toml_path = os.path.join(env_dir, entry, "service.toml")
            if not os.path.isfile(toml_path):
                continue
            try:
                svc = self.env["kensei2.sandbox"]._parse_service_toml(toml_path)
            except Exception:
                continue
            env_var = svc.get("env_var_name")
            port = svc.get("port")
            name = svc.get("name", entry)
            if env_var and port:
                env_vars[env_var] = "http://%s:%s" % (name, port)
        return env_vars

    def build_trajectory_json(self):
        self.ensure_one()
        all_turns = self._get_all_turns().sorted("turn_number")

        meta_info = {
            "task_type": self._slugify_task_type(),
            "task_description": self.seed_prompt or self.task_id or "",
            "task_completion_status": "success",
            "system_prompt": self.system_prompt or "",
            "platform": "macOS",
            "multimodal_metadata": self._build_multimodal_metadata(),
            "input_files": self._build_input_files_manifest(),
            "output_artifacts": self._build_output_artifacts(),
        }

        messages = self._trajectory_from_ws()
        if messages:
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = self._build_trajectory_fallback()

        messages = _unwrap_trajectory_messages(messages)

        from .kensei2_sandbox import _replace_inline_media_with_s3
        task_id = self.task_id or str(self.id)
        messages = _replace_inline_media_with_s3(messages, task_id, self.env)

        return {
            "schema_version": "1.0.0",
            "meta_info": meta_info,
            "messages": messages,
        }

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
            .get_param("kensei2.deployment_mode", "local")
            .strip()
        )

    @api.model
    def _cron_reconcile_sandboxes(self):
        self.env["kensei2.sandbox"]._cron_reconcile()
        self._reconcile_batch_status()

    @api.model
    def _reconcile_batch_status(self):
        # ── Phase 1: starting → ready ────────────────────────────────
        tasks = self.sudo().search([("batch_status", "=", "starting")])
        for task in tasks:
            sandboxes = task.sandbox_ids
            if not sandboxes:
                continue
            still_starting = sandboxes.filtered(
                lambda s: s.docker_status == "starting"
            )
            if still_starting:
                continue
            running = sandboxes.filtered(lambda s: s.docker_status == "running")
            if running:
                task.write({"batch_status": "ready"})
                _logger.info(
                    "[BATCH-RECONCILE] task=%s → ready (%d running, %d total)",
                    task.id, len(running), len(sandboxes),
                )
            else:
                task.write({
                    "batch_status": "error",
                    "batch_error": "All sandboxes failed to deploy.",
                    "batch_completed_at": fields.Datetime.now(),
                })
                _logger.warning(
                    "[BATCH-RECONCILE] task=%s → error (0 running, %d total)",
                    task.id, len(sandboxes),
                )

        # ── Phase 2: running → stopping / done ───────────────────────
        # A sandbox is "still working" if docker_status == running AND
        # session_status != completed.  Once every sandbox has finished
        # its prompt (completed / error / stopped), the batch is done.
        for task in self.sudo().search([("batch_status", "=", "running")]):
            sandboxes = task.sandbox_ids
            if not sandboxes:
                continue
            still_working = sandboxes.filtered(
                lambda s: s.docker_status == "running"
                and s.session_status != "completed"
            )
            if still_working:
                continue
            all_stopped = all(
                s.docker_status in ("stopped", "error") for s in sandboxes
            )
            completed = sandboxes.filtered(
                lambda s: s.session_status == "completed"
            )
            failed = sandboxes.filtered(
                lambda s: s.docker_status == "error"
            )
            if all_stopped:
                final = "done" if completed else "error"
                vals = {
                    "batch_status": final,
                    "batch_completed_at": fields.Datetime.now(),
                }
                if failed:
                    vals["batch_error"] = "%d sandbox(es) failed." % len(failed)
                task.write(vals)
                _logger.info(
                    "[BATCH-RECONCILE] task=%s → %s (%d completed, %d failed, %d total)",
                    task.id, final, len(completed), len(failed), len(sandboxes),
                )
            else:
                # Prompt work done but pods still alive → trigger stop
                task.write({"batch_status": "stopping"})
                _logger.info(
                    "[BATCH-RECONCILE] task=%s → stopping (prompt done, pods still alive)",
                    task.id,
                )

        # ── Phase 3: stopping → done ─────────────────────────────────
        for task in self.sudo().search([("batch_status", "=", "stopping")]):
            sandboxes = task.sandbox_ids
            if not sandboxes:
                continue
            still_alive = sandboxes.filtered(
                lambda s: s.docker_status in ("starting", "running")
            )
            if still_alive:
                continue
            completed = sandboxes.filtered(
                lambda s: s.session_status == "completed"
            )
            failed = sandboxes.filtered(
                lambda s: s.docker_status == "error"
            )
            final = "done" if completed else "error"
            vals = {
                "batch_status": final,
                "batch_completed_at": fields.Datetime.now(),
            }
            if failed:
                vals["batch_error"] = "%d sandbox(es) failed." % len(failed)
            task.write(vals)
            _logger.info(
                "[BATCH-RECONCILE] task=%s → %s (%d completed, %d failed, %d total)",
                task.id, final, len(completed), len(failed), len(sandboxes),
            )

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
        task = self.browse(task_id)
        if not task.exists():
            return {"skip": True, "reason": "not_found"}

        if task.auto_process_status != "queued":
            return {"skip": True, "reason": "status_%s" % task.auto_process_status}

        self.env.cr.execute(
            "UPDATE kensei2_kensei2 SET auto_process_status = 'processing' "
            "WHERE id = %s AND auto_process_status = 'queued' RETURNING id",
            [task_id],
        )
        claimed = self.env.cr.fetchone()
        if not claimed:
            return {"skip": True, "reason": "already_claimed"}

        task.invalidate_recordset()

        if task.batch_status in ("starting", "running", "stopping"):
            return {"skip": True, "reason": "batch_active"}

        prompt = (task.initial_prompt or "").strip()
        if not prompt:
            return {"skip": True, "reason": "no_prompt"}

        return {
            "task_id": task_id,
            "initial_prompt": prompt,
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


class Kensei2Turn(models.Model):
    _name = "kensei2.turn"
    _description = "Kensei2 Turn"
    _order = "turn_number asc, id asc"

    sandbox_id = fields.Many2one(
        "kensei2.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    kensei2_id = fields.Many2one(related="sandbox_id.kensei2_id", store=True, readonly=True)
    employee_id = fields.Many2one(
        related="kensei2_id.employee_id", store=True, readonly=True
    )
    employee_ids = fields.Many2many(
        related="kensei2_id.employee_ids", readonly=True
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


class Kensei2Taxonomy(models.Model):
    _name = "kensei2.taxonomy"
    _description = "Kensei2 Taxonomy"

    name = fields.Char(string="Name", required=True, unique=True)
