"""Rollout replay storage for debugging and analysis."""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from src.core.schemas import Trajectory

log = logging.getLogger(__name__)


class RolloutReplayStore:
    def __init__(
        self, output_dir: str | Path, max_stored: int = 10000, compress: bool = True
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._max_stored = max_stored
        self._compress = compress
        self._count = 0
        self._count = self._count_existing()

    def store(self, trajectories: list[Trajectory], step: int) -> None:
        path = self._step_path(step)
        data = [t.model_dump() for t in trajectories]
        serialized = json.dumps(data).encode("utf-8")

        if self._compress:
            with gzip.open(path, "wb") as f:
                f.write(serialized)
        else:
            path.write_bytes(serialized)

        self._count += len(trajectories)
        self._evict_old()
        log.debug("Stored %d trajectories for step %d", len(trajectories), step)

    def load_step(self, step: int) -> list[Trajectory]:
        path = self._step_path(step)
        if not path.exists():
            return []

        if self._compress:
            with gzip.open(path, "rb") as f:
                raw = f.read()
        else:
            raw = path.read_bytes()

        data = json.loads(raw)
        return [Trajectory.model_validate(item) for item in data]

    def load_failures(self, last_n_steps: int = 10) -> list[Trajectory]:
        steps = self.get_stored_steps()
        recent = steps[-last_n_steps:] if len(steps) > last_n_steps else steps
        failures: list[Trajectory] = []
        for step in recent:
            trajectories = self.load_step(step)
            failures.extend(t for t in trajectories if not t.is_success)
        return failures

    def load_successes(self, last_n_steps: int = 10) -> list[Trajectory]:
        steps = self.get_stored_steps()
        recent = steps[-last_n_steps:] if len(steps) > last_n_steps else steps
        successes: list[Trajectory] = []
        for step in recent:
            trajectories = self.load_step(step)
            successes.extend(t for t in trajectories if t.is_success)
        return successes

    def get_stored_steps(self) -> list[int]:
        steps: list[int] = []
        suffix = ".json.gz" if self._compress else ".json"
        for path in self._output_dir.iterdir():
            if path.name.startswith("step_") and path.name.endswith(suffix):
                stem = path.name.removeprefix("step_").removesuffix(suffix)
                try:
                    steps.append(int(stem))
                except ValueError:
                    continue
        steps.sort()
        return steps

    def _evict_old(self) -> None:
        steps = self.get_stored_steps()
        while self._count > self._max_stored and steps:
            oldest_step = steps.pop(0)
            path = self._step_path(oldest_step)
            if path.exists():
                try:
                    trajectories = self.load_step(oldest_step)
                    self._count -= len(trajectories)
                except Exception:
                    self._count -= 1
                path.unlink()
                log.debug("Evicted step %d", oldest_step)

    def _step_path(self, step: int) -> Path:
        suffix = ".json.gz" if self._compress else ".json"
        return self._output_dir / f"step_{step}{suffix}"

    def _count_existing(self) -> int:
        total = 0
        for step in self.get_stored_steps():
            path = self._step_path(step)
            if path.exists():
                try:
                    if self._compress:
                        with gzip.open(path, "rb") as f:
                            data = json.loads(f.read())
                    else:
                        data = json.loads(path.read_bytes())
                    total += len(data)
                except (json.JSONDecodeError, OSError):
                    continue
        return total
