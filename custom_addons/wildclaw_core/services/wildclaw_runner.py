"""Bridge from Odoo to the vendored WildClawBench library.

Public API used by wrapper addons (kensei_wildclaw / skoll_wildclaw / talos_wildclaw):

    run_task(env, sandbox, *, prompt, output_dir, progress_callback=None)
        Spawns an OpenClaw container via WildClawBench, runs the prompt, returns the
        AgentExecution. Sandbox model fields are read at start and trajectory result is
        written back at finish.

    collect_usage(env, sandbox, execution, output_dir) -> dict
        Returns token-usage dict suitable for writing onto kensei/skoll/talos token
        counter fields. Safe to call after an error AgentExecution.

The function is fully synchronous so wrapper code can call it from inside a
ThreadPoolExecutor worker.  Odoo registry handling (Registry(db).cursor() retry on
serialize-conflict) belongs in wrapper code, not here, so this bridge is safe to use
from contexts that do not hold an Odoo cursor.
"""

import logging
import os
import sys
import threading
from pathlib import Path

_logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "wildclawbench"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))


def _import_api():
    from src.api import (  # type: ignore
        build_task_spec_from_dict,
        run_task_programmatic,
        collect_usage_programmatic,
        TaskSpecValidationError,
    )

    return {
        "build_task_spec_from_dict": build_task_spec_from_dict,
        "run_task_programmatic": run_task_programmatic,
        "collect_usage_programmatic": collect_usage_programmatic,
        "TaskSpecValidationError": TaskSpecValidationError,
    }


_RUN_LOCK = threading.Lock()
_IN_FLIGHT: set = set()

_DEFAULT_SKILLS = ("video-frames", "agent-browser", "self-improving-agent-3.0.5")


def _default_skills() -> list:
    skills_dir = _VENDOR_DIR / "skills"
    if not skills_dir.is_dir():
        return []
    return [s for s in _DEFAULT_SKILLS if (skills_dir / s).is_dir()]


def _build_task_dict_from_sandbox(env, sandbox, prompt: str) -> dict:
    task_record = sandbox.task_id if hasattr(sandbox, "task_id") and sandbox.task_id else None
    if task_record is None and hasattr(sandbox, "task_ref"):
        task_record = sandbox.task_ref
    if task_record is None:
        raise ValueError(
            f"sandbox {sandbox._name}#{sandbox.id} has no task_id relation; "
            "wrapper must set _inherit on wildclaw.sandbox_base and provide task_id."
        )

    persona = task_record.persona_id
    media_attachments = task_record.media_attachment_ids if hasattr(task_record, "media_attachment_ids") else []

    workspace_path = _ensure_workspace(env, sandbox, persona, media_attachments)
    models_config = _build_models_config(env, persona)

    return {
        "task_id": f"{sandbox._name}_{sandbox.id}_{sandbox.model_type}",
        "prompt": prompt or (task_record.initial_prompt or task_record.seed_prompt or ""),
        "workspace_path": str(workspace_path),
        "timeout_seconds": int(env["ir.config_parameter"].sudo().get_param("wildclaw.task_timeout", "1200")),
        "model": _resolve_model_string(sandbox.model_type),
        "thinking": "on" if persona and persona.litellm_config_yaml and "thinking" in (persona.litellm_config_yaml or "") else "off",
        "models_config": models_config,
        "lobster": {
            "name": persona.name if persona else "default",
            "workspace": _persona_workspace_dir(env, persona) if persona else None,
            "env": [],
        },
        "skills": _default_skills(),
        "skills_path": str(_VENDOR_DIR / "skills"),
        "warmup": "",
        "env": {},
        "category": task_record.task_type or "default",
        "task": {
            "id": task_record.task_id,
            "type": task_record.task_type,
            "difficulty": task_record.difficulty,
            "system_prompt": task_record.system_prompt or "",
        },
    }


def _ensure_workspace(env, sandbox, persona, media_attachments):
    base = Path(env["ir.config_parameter"].sudo().get_param("wildclaw.sandbox_dir", "/tmp/wildclaw_workspaces"))
    base.mkdir(parents=True, exist_ok=True)
    ws = base / f"{sandbox._name.replace('.', '_')}_{sandbox.id}"
    ws.mkdir(parents=True, exist_ok=True)
    if persona and persona.soul_md:
        (ws / "SOUL.md").write_text(persona.soul_md)
    if persona and persona.memory_md:
        (ws / "MEMORY.md").write_text(persona.memory_md)
    if persona and persona.agents_md:
        (ws / "AGENTS.md").write_text(persona.agents_md)
    media_dir = ws / "media"
    if media_attachments:
        media_dir.mkdir(exist_ok=True)
        for att in media_attachments:
            if att.s3_url:
                (media_dir / att.name).write_text(att.s3_url)
    return ws


def _persona_workspace_dir(env, persona):
    if not persona:
        return None
    base = Path(env["ir.config_parameter"].sudo().get_param("wildclaw.sandbox_dir", "/tmp/wildclaw_workspaces"))
    p = base / "personas" / (persona.name or "default")
    p.mkdir(parents=True, exist_ok=True)
    if persona.soul_md:
        (p / "SOUL.md").write_text(persona.soul_md)
    if persona.memory_md:
        (p / "MEMORY.md").write_text(persona.memory_md)
    return str(p)


def _build_models_config(env, persona):
    if persona and persona.litellm_config_yaml:
        return None
    arn = env["ir.config_parameter"].sudo().get_param("wildclaw.bedrock_inference_arn", "")
    region = env["ir.config_parameter"].sudo().get_param("wildclaw.bedrock_region", "us-east-1")
    if not arn:
        return None
    return {
        "models": [
            {
                "model_name": "claude-opus-4.7",
                "litellm_params": {
                    "model": f"bedrock/converse/{arn}",
                    "aws_region_name": region,
                },
            }
        ]
    }


def _resolve_model_string(model_type: str) -> str:
    return {
        "claude": "openrouter/anthropic/claude-sonnet-4.6",
        "gpt": "openai/gpt-5.5",
    }.get(model_type, "openrouter/anthropic/claude-sonnet-4.6")


def run_task(env, sandbox, *, prompt: str = "", output_dir=None, progress_callback=None):
    api = _import_api()
    task_dict = _build_task_dict_from_sandbox(env, sandbox, prompt)
    out_dir = Path(output_dir) if output_dir else Path(env["ir.config_parameter"].sudo().get_param(
        "wildclaw.sandbox_dir", "/tmp/wildclaw_workspaces"
    )) / "output" / f"{sandbox._name.replace('.', '_')}_{sandbox.id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = api["build_task_spec_from_dict"](task_dict, output_dir=out_dir)
    key = f"{sandbox._name}#{sandbox.id}"
    with _RUN_LOCK:
        if key in _IN_FLIGHT:
            raise RuntimeError(f"run_task already in flight for {key}")
        _IN_FLIGHT.add(key)
    try:
        execution = api["run_task_programmatic"](spec, backend="openclaw", progress_callback=progress_callback)
    finally:
        with _RUN_LOCK:
            _IN_FLIGHT.discard(key)
    return execution


def collect_usage(env, sandbox, execution, output_dir=None) -> dict:
    api = _import_api()
    out_dir = Path(output_dir) if output_dir else Path(env["ir.config_parameter"].sudo().get_param(
        "wildclaw.sandbox_dir", "/tmp/wildclaw_workspaces"
    )) / "output" / f"{sandbox._name.replace('.', '_')}_{sandbox.id}"
    spec_dict = {
        "task_id": f"{sandbox._name}_{sandbox.id}_{sandbox.model_type}",
        "prompt": "",
        "workspace_path": str(out_dir),
    }
    try:
        spec = api["build_task_spec_from_dict"](spec_dict, output_dir=out_dir)
        return api["collect_usage_programmatic"](spec, execution) or {}
    except Exception as exc:
        _logger.warning("collect_usage failed for %s#%s: %s", sandbox._name, sandbox.id, exc)
        return {}


def is_wildclawbench_available() -> bool:
    try:
        _import_api()
        return True
    except Exception as exc:
        _logger.info("WildClawBench library unavailable: %s", exc)
        return False
