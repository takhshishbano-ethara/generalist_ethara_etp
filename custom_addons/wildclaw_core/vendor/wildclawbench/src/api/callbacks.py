"""Progress callback primitives for embedding WildClawBench in long-running hosts (Odoo, services, etc.).

WildClawBench's CLI runner prints status to stdout. When embedded inside Odoo / RabbitMQ workers /
SSE handlers, we instead invoke a caller-provided `progress_callback` with structured events.

Event shape is intentionally simple (a plain dict) so it can be JSON-serialized and pushed to
bus.bus / SSE / RabbitMQ without further transformation on the caller side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

ProgressCallback = Callable[[dict], None]


# Stable event type enum (string constants — callers may switch on these)
EV_TASK_STARTED = "task_started"
EV_CONTAINER_STARTED = "container_started"
EV_CONTAINER_READY = "container_ready"
EV_WORKSPACE_PREPARED = "workspace_prepared"
EV_SKILLS_INSTALLED = "skills_installed"
EV_MODELS_INJECTED = "models_injected"
EV_GATEWAY_STARTED = "gateway_started"
EV_AGENT_INVOKED = "agent_invoked"
EV_AGENT_STDOUT = "agent_stdout"
EV_AGENT_STDERR = "agent_stderr"
EV_AGENT_FINISHED = "agent_finished"
EV_TRANSCRIPT_READY = "transcript_ready"
EV_GRADING_STARTED = "grading_started"
EV_GRADING_PROGRESS = "grading_progress"
EV_GRADING_FINISHED = "grading_finished"
EV_TASK_FINISHED = "task_finished"
EV_TASK_ERROR = "task_error"


@dataclass
class ProgressEvent:
    """Structured progress event. Convertible to dict via `to_dict()` for serialization."""
    event: str
    task_id: str
    timestamp: float = field(default_factory=time.time)
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class CallbackEmitter:
    """Helper that callers can hold to emit structured events without dealing with dict shapes.

    Example:
        def my_cb(event: dict):
            self.env['bus.bus']._sendone(partner, 'wildclaw/progress', event)

        emitter = CallbackEmitter(task_id='abc123', callback=my_cb)
        emitter.emit(EV_TASK_STARTED, prompt=prompt, model=model)
    """

    def __init__(self, task_id: str, callback: Optional[ProgressCallback] = None):
        self.task_id = task_id
        self._callback = callback

    def emit(self, event: str, **payload: Any) -> None:
        if not self._callback:
            return
        try:
            self._callback(ProgressEvent(event=event, task_id=self.task_id, payload=payload).to_dict())
        except Exception:  # noqa: BLE001 — never let callback errors abort agent execution
            import logging
            logging.getLogger(__name__).exception(
                "WildClawBench progress callback raised; suppressing to keep agent run alive."
            )

    def __bool__(self) -> bool:
        return self._callback is not None
