"""Kill condition detectors for training stability."""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass
from enum import Enum

from src.core.config import MonitoringConfig
from src.core.schemas import TrainingMetrics

log = logging.getLogger(__name__)


class KillReason(Enum):
    ECHO_TRAP = "echo_trap"
    GRADIENT_EXPLOSION = "gradient_explosion"
    CATASTROPHIC_FORGETTING = "catastrophic_forgetting"
    DEAD_TRAINING = "dead_training"
    MODE_COLLAPSE = "mode_collapse"
    REWARD_HACKING = "reward_hacking"
    OOM = "oom"


@dataclass
class KillAction:
    reason: KillReason
    severity: str  # "warning", "recoverable", "fatal"
    message: str
    suggested_action: str


class KillConditionMonitor:
    def __init__(self, config: MonitoringConfig):
        self._config = config
        self._reward_history: deque[float] = deque(maxlen=config.echo_trap_window)
        self._grad_history: deque[float] = deque(maxlen=config.grad_explosion_window)
        self._eval_history: list[float] = []
        self._success_history: deque[float] = deque(maxlen=config.dead_training_window)
        self._peak_eval: float = 0.0
        self._cooldowns: dict[KillReason, int] = {}
        self._cooldown_duration: int = 10  # steps to suppress after triggering

    def check(self, metrics: TrainingMetrics) -> KillAction | None:
        """Run all kill condition checks. Returns first triggered or None."""
        self._update_histories(metrics)
        self._tick_cooldowns()

        checks = [
            self._check_echo_trap,
            self._check_gradient_explosion,
            self._check_catastrophic_forgetting,
            self._check_dead_training,
            self._check_mode_collapse,
        ]
        for check_fn in checks:
            action = check_fn(metrics)
            if action is not None:
                if self._is_on_cooldown(action.reason):
                    continue
                log.warning("Kill condition triggered: %s", action.reason.value)
                if action.severity == "recoverable":
                    self._cooldowns[action.reason] = self._cooldown_duration
                return action
        return None

    def _tick_cooldowns(self) -> None:
        expired = [r for r, c in self._cooldowns.items() if c <= 0]
        for r in expired:
            del self._cooldowns[r]
        for r in self._cooldowns:
            self._cooldowns[r] -= 1

    def _is_on_cooldown(self, reason: KillReason) -> bool:
        return reason in self._cooldowns and self._cooldowns[reason] > 0

    def _update_histories(self, metrics: TrainingMetrics) -> None:
        self._reward_history.append(metrics.reward_variance)
        self._grad_history.append(metrics.grad_norm)
        self._success_history.append(metrics.success_rate)

        if metrics.eval_pass_at_1 is not None:
            self._eval_history.append(metrics.eval_pass_at_1)
            if metrics.eval_pass_at_1 > self._peak_eval:
                self._peak_eval = metrics.eval_pass_at_1

    def _check_echo_trap(self, metrics: TrainingMetrics) -> KillAction | None:
        window = self._config.echo_trap_window
        if len(self._reward_history) < window:
            return None

        avg_variance = statistics.mean(self._reward_history)
        if avg_variance < self._config.echo_trap_threshold:
            return KillAction(
                reason=KillReason.ECHO_TRAP,
                severity="recoverable",
                message=(
                    f"Reward variance {avg_variance:.4f} below threshold "
                    f"{self._config.echo_trap_threshold} for {window} steps"
                ),
                suggested_action="reduce difficulty, increase temperature",
            )
        return None

    def _check_gradient_explosion(self, metrics: TrainingMetrics) -> KillAction | None:
        window = self._config.grad_explosion_window
        if len(self._grad_history) < window:
            return None

        threshold = self._config.grad_explosion_threshold
        if all(g > threshold for g in self._grad_history):
            return KillAction(
                reason=KillReason.GRADIENT_EXPLOSION,
                severity="recoverable",
                message=(
                    f"All {window} recent grad norms > {threshold}: "
                    f"{list(self._grad_history)}"
                ),
                suggested_action="reduce lr by 10x, revert 10 steps",
            )
        return None

    def _check_catastrophic_forgetting(
        self, metrics: TrainingMetrics
    ) -> KillAction | None:
        if metrics.eval_pass_at_1 is None:
            return None
        if self._peak_eval == 0.0:
            return None

        drop = self._peak_eval - metrics.eval_pass_at_1
        if drop > self._config.forgetting_threshold:
            return KillAction(
                reason=KillReason.CATASTROPHIC_FORGETTING,
                severity="fatal",
                message=(
                    f"Eval pass@1 dropped from {self._peak_eval:.3f} to "
                    f"{metrics.eval_pass_at_1:.3f} (drop={drop:.3f} > "
                    f"threshold={self._config.forgetting_threshold})"
                ),
                suggested_action="STOP, use peak checkpoint",
            )
        return None

    def _check_dead_training(self, metrics: TrainingMetrics) -> KillAction | None:
        window = self._config.dead_training_window
        if len(self._success_history) < window:
            return None

        if all(s == 0.0 for s in self._success_history):
            return KillAction(
                reason=KillReason.DEAD_TRAINING,
                severity="fatal",
                message=(
                    f"Success rate 0.0 for {window} consecutive steps, "
                    f"no learning signal detected"
                ),
                suggested_action="STOP, no learning signal",
            )
        return None

    def _check_mode_collapse(self, metrics: TrainingMetrics) -> KillAction | None:
        ngram_overlap = getattr(metrics, "ngram_overlap", None)
        if ngram_overlap is None:
            return None

        threshold = self._config.mode_collapse_ngram_overlap
        if ngram_overlap > threshold:
            return KillAction(
                reason=KillReason.MODE_COLLAPSE,
                severity="warning",
                message=(
                    f"N-gram overlap {ngram_overlap:.3f} > threshold {threshold}"
                ),
                suggested_action="increase temperature, add entropy bonus",
            )
        return None

    def report_oom(self) -> KillAction:
        return KillAction(
            reason=KillReason.OOM,
            severity="recoverable",
            message="Out of memory detected during training step",
            suggested_action="reduce batch size or sequence length, retry",
        )

    def report_reward_hacking(self, evidence: str) -> KillAction:
        return KillAction(
            reason=KillReason.REWARD_HACKING,
            severity="fatal",
            message=f"Reward hacking detected: {evidence}",
            suggested_action="STOP, investigate reward function",
        )

    def reset(self) -> None:
        self._reward_history.clear()
        self._grad_history.clear()
        self._eval_history.clear()
        self._success_history.clear()
        self._peak_eval = 0.0
