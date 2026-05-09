"""Per-step metric computation and logging."""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path

from src.core.schemas import TrainingMetrics, Trajectory

log = logging.getLogger(__name__)


class MetricsTracker:
    def __init__(
        self, output_dir: str | Path, window_size: int = 50, use_wandb: bool = False
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._window_size = window_size
        self._use_wandb = use_wandb
        self._history: list[TrainingMetrics] = []
        self._last_saved_idx: int = 0
        self._reward_window: deque[float] = deque(maxlen=window_size)
        self._success_window: deque[float] = deque(maxlen=window_size)
        self._solved_tasks: set[str] = set()
        self._wandb_run = None

        if self._use_wandb:
            self._init_wandb()

    def record_step(
        self,
        step: int,
        trajectories: list[Trajectory],
        grad_norm: float,
        learning_rate: float,
        curriculum_phase: int,
    ) -> TrainingMetrics:
        if not trajectories:
            metrics = TrainingMetrics(
                step=step,
                success_rate=0.0,
                mask_rate=0.0,
                avg_episode_length=0.0,
                reward_variance=0.0,
                grad_norm=grad_norm,
                learning_rate=learning_rate,
                unique_tasks_solved=len(self._solved_tasks),
                curriculum_phase=curriculum_phase,
                total_rollouts=0,
                timestamp=time.time(),
            )
            self._history.append(metrics)
            self._log_metrics(metrics)
            return metrics

        rewards = [t.reward for t in trajectories]
        successes = [1.0 if t.is_success else 0.0 for t in trajectories]
        masks = [1.0 if t.mask else 0.0 for t in trajectories]
        episode_lengths = [float(t.episode_length) for t in trajectories]

        for t in trajectories:
            self._reward_window.append(t.reward)
            self._success_window.append(1.0 if t.is_success else 0.0)
            if t.is_success:
                self._solved_tasks.add(t.task_id)

        n = len(trajectories)
        mean_reward = sum(rewards) / n
        reward_var = sum((r - mean_reward) ** 2 for r in rewards) / n

        metrics = TrainingMetrics(
            step=step,
            success_rate=sum(successes) / n,
            mask_rate=sum(masks) / n,
            avg_episode_length=sum(episode_lengths) / n,
            reward_variance=reward_var,
            grad_norm=grad_norm,
            learning_rate=learning_rate,
            unique_tasks_solved=len(self._solved_tasks),
            curriculum_phase=curriculum_phase,
            total_rollouts=n,
            timestamp=time.time(),
        )

        self._history.append(metrics)
        self._log_metrics(metrics)

        if self._use_wandb:
            self._wandb_log(metrics)

        return metrics

    def get_recent_metrics(self, n: int = 10) -> list[TrainingMetrics]:
        return self._history[-n:]

    def get_reward_variance(self) -> float:
        if not self._reward_window:
            return 0.0
        mean = sum(self._reward_window) / len(self._reward_window)
        return sum((r - mean) ** 2 for r in self._reward_window) / len(self._reward_window)

    def get_success_rate(self) -> float:
        if not self._success_window:
            return 0.0
        return sum(self._success_window) / len(self._success_window)

    def get_unique_tasks_solved(self) -> int:
        return len(self._solved_tasks)

    def save_to_jsonl(self) -> Path:
        output_path = self._output_dir / "metrics.jsonl"
        with output_path.open("a") as f:
            for m in self._history[self._last_saved_idx:]:
                f.write(m.model_dump_json() + "\n")
        self._last_saved_idx = len(self._history)
        return output_path

    def _log_metrics(self, metrics: TrainingMetrics) -> None:
        log.info(
            "step=%d success_rate=%.3f reward_var=%.4f grad_norm=%.4f "
            "unique_solved=%d phase=%d rollouts=%d",
            metrics.step,
            metrics.success_rate,
            metrics.reward_variance,
            metrics.grad_norm,
            metrics.unique_tasks_solved,
            metrics.curriculum_phase,
            metrics.total_rollouts,
        )

    def _init_wandb(self) -> None:
        try:
            import wandb
            if wandb.run is None:
                self._wandb_run = wandb.init(project="milo-rl", resume="allow")
            else:
                self._wandb_run = wandb.run
        except ImportError:
            log.warning("wandb not installed, disabling wandb logging")
            self._use_wandb = False

    def _wandb_log(self, metrics: TrainingMetrics) -> None:
        try:
            import wandb
            wandb.log(
                {
                    "train/success_rate": metrics.success_rate,
                    "train/mask_rate": metrics.mask_rate,
                    "train/avg_episode_length": metrics.avg_episode_length,
                    "train/reward_variance": metrics.reward_variance,
                    "train/grad_norm": metrics.grad_norm,
                    "train/learning_rate": metrics.learning_rate,
                    "train/unique_tasks_solved": metrics.unique_tasks_solved,
                    "train/curriculum_phase": metrics.curriculum_phase,
                    "train/total_rollouts": metrics.total_rollouts,
                },
                step=metrics.step,
            )
        except Exception as e:
            log.warning("wandb log failed: %s", e)
