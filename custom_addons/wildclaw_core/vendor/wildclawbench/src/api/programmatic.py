"""In-process Python API for running WildClawBench tasks from embedding hosts (Odoo, services).

CLI users invoke `eval/run_batch.py`. Embedding hosts (Odoo controllers, RabbitMQ consumers, SSE
streams) instead call `run_task_programmatic(spec, ...)` and receive a synchronous result plus a
stream of structured progress events.

This module is intentionally thin — it picks the harness, wires the progress callback into
docker_utils + grading + harness lifecycle, and returns the unified `AgentExecution` result.

Currently only the OpenClaw harness is wired (per `m0050` scope: "We only want openclaw support").
Other harnesses (ClaudeCode/Codex/Hermes) are not vendored.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any, Optional

from ..agents.base import AgentExecution, AgentTaskSpec, BaseAgent
from .callbacks import (
    EV_AGENT_FINISHED,
    EV_AGENT_INVOKED,
    EV_TASK_ERROR,
    EV_TASK_FINISHED,
    EV_TASK_STARTED,
    CallbackEmitter,
    ProgressCallback,
)

_logger = logging.getLogger(__name__)


# Backend name -> lazy importer. We import lazily so wildclaw_core can be loaded even if some
# harnesses' optional dependencies are missing in a given deployment.
_BACKEND_IMPORTERS: dict[str, Any] = {}


def _register_openclaw():
    from ..agents.openclaw.runner import OpenClawAgent  # type: ignore
    return OpenClawAgent


_BACKEND_IMPORTERS["openclaw"] = _register_openclaw


def get_supported_backends() -> list[str]:
    """Return the list of harness names currently wired into this build of WildClawBench."""
    return sorted(_BACKEND_IMPORTERS.keys())


def _load_backend(backend: str) -> type[BaseAgent]:
    if backend not in _BACKEND_IMPORTERS:
        raise ValueError(
            f"Unknown backend {backend!r}. Supported: {get_supported_backends()}"
        )
    return _BACKEND_IMPORTERS[backend]()


def run_task_programmatic(
    spec: AgentTaskSpec,
    *,
    backend: str = "openclaw",
    progress_callback: Optional[ProgressCallback] = None,
    extra_backend_kwargs: Optional[dict] = None,
) -> AgentExecution:
    """Run a single agent task, returning the full `AgentExecution`.

    This is the embedding entry point. Callers (typically `wildclaw_core.services.wildclaw_runner`)
    pass in a fully-populated AgentTaskSpec built from an Odoo task record, choose a harness, and
    receive progress events along the way.

    Args:
        spec: Built by `task_factory.build_task_spec_from_dict(...)`.
        backend: One of `get_supported_backends()`. Default 'openclaw'.
        progress_callback: Optional callable receiving dict-shaped events (see callbacks.py).
                          The callback runs synchronously inside the agent loop; keep it fast.
                          Exceptions raised by the callback are swallowed (logged) so they cannot
                          abort the agent run.
        extra_backend_kwargs: Reserved for backend-specific overrides (e.g. image tag).

    Returns:
        AgentExecution: The harness's complete execution record (elapsed_time, error,
                        gateway_proc, agent_proc). The transcript is on disk at the path
                        returned by `agent.prepare_grading_transcript(spec.task_id)`.

    Raises:
        Does NOT raise on agent-side errors — instead returns AgentExecution(error=...).
        Raises only on programmer errors (unknown backend, malformed spec).
    """
    emitter = CallbackEmitter(task_id=spec.task_id, callback=progress_callback)
    emitter.emit(
        EV_TASK_STARTED,
        backend=backend,
        model=spec.model,
        thinking=spec.thinking,
        timeout_seconds=spec.timeout_seconds,
        prompt_chars=len(spec.prompt),
    )

    try:
        agent_cls = _load_backend(backend)
        agent: BaseAgent = agent_cls(**(extra_backend_kwargs or {}))
    except Exception as exc:  # noqa: BLE001 — surface as task_error event
        emitter.emit(EV_TASK_ERROR, phase="backend_init", error=str(exc), traceback=traceback.format_exc())
        return AgentExecution(
            elapsed_time=0.0,
            error=f"Failed to load backend {backend!r}: {exc}",
            gateway_proc=None,
            agent_proc=None,
        )

    # Attach the emitter so harnesses that opt into streaming can use it.
    # (Harnesses that ignore it work unchanged; this is purely additive.)
    setattr(agent, "_wcb_emitter", emitter)

    emitter.emit(EV_AGENT_INVOKED, harness=type(agent).__name__)

    try:
        execution = agent.run_task(spec)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("WildClawBench backend %s raised during run_task", backend)
        emitter.emit(
            EV_TASK_ERROR,
            phase="run_task",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return AgentExecution(
            elapsed_time=0.0,
            error=f"Backend {backend} raised: {exc}",
            gateway_proc=None,
            agent_proc=None,
        )

    emitter.emit(
        EV_AGENT_FINISHED,
        elapsed_time=execution.elapsed_time,
        error=execution.error,
    )

    emitter.emit(
        EV_TASK_FINISHED,
        success=(execution.error is None),
        elapsed_time=execution.elapsed_time,
    )
    return execution


def collect_usage_programmatic(
    spec: AgentTaskSpec,
    execution: AgentExecution,
    *,
    backend: str = "openclaw",
) -> dict:
    """Extract token/cost usage from a completed run. Mirrors `BaseAgent.collect_usage`."""
    agent_cls = _load_backend(backend)
    agent: BaseAgent = agent_cls()
    return agent.collect_usage(
        task_id=spec.task_id,
        output_dir=spec.output_dir,
        elapsed_time=execution.elapsed_time,
    )
