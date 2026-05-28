"""Build `AgentTaskSpec` instances from in-memory dicts (no .md file needed).

The CLI runner (`eval/run_batch.py`) calls `task_parser.parse_task_md(path)` to load tasks from
.md files. When embedded inside Odoo, tasks live in Postgres rows (kensei_wildclaw.task,
skoll_wildclaw.task, talos_wildclaw.task). This module provides the equivalent constructor for
DB-sourced tasks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..agents.base import AgentTaskSpec

_logger = logging.getLogger(__name__)


# Required keys in the task dict. Extra keys are ignored, missing keys raise ValueError.
_REQUIRED_KEYS = ("task_id", "prompt", "workspace_path")
# Optional keys with defaults.
_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "task": {},
    "timeout_seconds": 1200,
    "model": "openrouter/anthropic/claude-sonnet-4.6",
    "thinking": "off",
    "models_config": None,
    "lobster": None,
    "skills": [],
    "skills_path": None,
    "warmup": "",
    "env": {},
    "category": "default",
}


class TaskSpecValidationError(ValueError):
    """Raised when the input dict doesn't satisfy AgentTaskSpec's contract."""


def build_task_spec_from_dict(
    task_dict: dict,
    *,
    output_dir: Path,
    system_prompt_prefix: Optional[str] = None,
) -> AgentTaskSpec:
    """Materialize an `AgentTaskSpec` from a plain dict (typically a serialized Odoo record).

    Expected shape::

        {
            "task_id":         str   (required) — stable run identifier, used for container name
            "prompt":          str   (required) — main user message sent to the agent
            "workspace_path":  str   (required) — host path mounted into container as /app:ro
            "task":            dict  (optional) — full task metadata (passed through to harness)
            "timeout_seconds": int   (optional, default 1200)
            "model":           str   (optional, default 'openrouter/anthropic/claude-sonnet-4.6')
            "thinking":        str   (optional, 'off' | 'on' | 'medium' | 'high')
            "models_config":   dict  (optional) — injected as openclaw.json `models` block
            "lobster":         dict  (optional) — {name, workspace, env} for personalized eval
            "skills":          list  (optional) — names of skill folders under skills_path
            "skills_path":     str   (optional) — root dir containing skill subdirs
            "warmup":          str   (optional) — bash commands run inside container pre-prompt
            "env":             dict  (optional) — additional env vars for the container
            "category":        str   (optional) — used in output dir layout
        }

    Args:
        task_dict: Dict produced by an Odoo model (e.g. `kensei_wildclaw.task._to_wildclaw_dict()`).
        output_dir: Host directory where per-run artifacts (score.json, usage.json,
                    transcript copy) will be written.
        system_prompt_prefix: Optional prefix prepended to `prompt` (mirrors run_batch.py default
                              "You are an expert in a restricted, non-interactive environment..."
                              behaviour). Pass None to skip.

    Returns:
        Fully-populated AgentTaskSpec ready to pass to `run_task_programmatic`.

    Raises:
        TaskSpecValidationError: If a required key is missing or has the wrong type.
    """
    missing = [k for k in _REQUIRED_KEYS if k not in task_dict or task_dict[k] is None]
    if missing:
        raise TaskSpecValidationError(
            f"task_dict missing required keys: {missing}. Got keys: {sorted(task_dict.keys())}"
        )

    merged: dict[str, Any] = {**_OPTIONAL_DEFAULTS, **task_dict}

    prompt = str(merged["prompt"])
    if system_prompt_prefix:
        prompt = f"{system_prompt_prefix.rstrip()}\n\n{prompt}"

    workspace_path = Path(str(merged["workspace_path"]))
    if not workspace_path.exists():
        _logger.warning(
            "workspace_path %s does not exist; the agent run will likely fail at container start.",
            workspace_path,
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    spec = AgentTaskSpec(
        task_id=str(merged["task_id"]),
        task=dict(merged["task"] or {}),
        workspace_path=workspace_path,
        prompt=prompt,
        timeout_seconds=int(merged["timeout_seconds"]),
        output_dir=output_dir,
        model=str(merged["model"]),
        thinking=str(merged["thinking"]),
        models_config=merged["models_config"],
        lobster=merged["lobster"],
    )
    # Stash extra context on the spec so harnesses can read it.
    # (AgentTaskSpec is a frozen-ish dataclass — we attach via setattr for forward-compat.)
    setattr(spec, "skills", list(merged["skills"] or []))
    setattr(spec, "skills_path", merged["skills_path"])
    setattr(spec, "warmup", str(merged["warmup"] or ""))
    setattr(spec, "env", dict(merged["env"] or {}))
    setattr(spec, "category", str(merged["category"]))
    return spec
