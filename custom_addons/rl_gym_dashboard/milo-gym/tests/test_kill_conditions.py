from __future__ import annotations

import pytest

from src.monitoring.kill_conditions import KillConditionMonitor, KillReason, KillAction
from src.core.config import MonitoringConfig
from src.core.schemas import TrainingMetrics


@pytest.fixture
def config() -> MonitoringConfig:
    return MonitoringConfig(
        echo_trap_threshold=0.02,
        echo_trap_window=5,
        grad_explosion_threshold=100.0,
        grad_explosion_window=3,
        forgetting_threshold=0.05,
        dead_training_window=4,
        mode_collapse_ngram_overlap=0.9,
    )


@pytest.fixture
def monitor(config: MonitoringConfig) -> KillConditionMonitor:
    return KillConditionMonitor(config)


def _make_metrics(
    step: int = 1,
    success_rate: float = 0.3,
    reward_variance: float = 0.1,
    grad_norm: float = 1.0,
    eval_pass_at_1: float | None = None,
) -> TrainingMetrics:
    return TrainingMetrics(
        step=step,
        success_rate=success_rate,
        mask_rate=0.1,
        avg_episode_length=5.0,
        reward_variance=reward_variance,
        grad_norm=grad_norm,
        learning_rate=3e-5,
        unique_tasks_solved=10,
        curriculum_phase=1,
        total_rollouts=100,
        eval_pass_at_1=eval_pass_at_1,
    )


class TestKillConditionMonitor:
    def test_no_kill_normal_metrics(self, monitor: KillConditionMonitor):
        result = None
        for i in range(10):
            result = monitor.check(_make_metrics(step=i, reward_variance=0.1 + i * 0.01))
        assert result is None

    def test_echo_trap_detected(self, monitor: KillConditionMonitor):
        triggered = None
        for i in range(10):
            result = monitor.check(_make_metrics(step=i, reward_variance=0.001))
            if result is not None and triggered is None:
                triggered = result
        assert triggered is not None
        assert triggered.reason == KillReason.ECHO_TRAP
        assert triggered.severity == "recoverable"

    def test_gradient_explosion_detected(self, monitor: KillConditionMonitor):
        triggered = None
        for i in range(5):
            result = monitor.check(
                _make_metrics(step=i, grad_norm=200.0, reward_variance=0.1 + i * 0.05)
            )
            if result is not None and triggered is None:
                triggered = result
        assert triggered is not None
        assert triggered.reason == KillReason.GRADIENT_EXPLOSION

    def test_catastrophic_forgetting(self, monitor: KillConditionMonitor):
        monitor.check(_make_metrics(step=1, eval_pass_at_1=0.5, reward_variance=0.2))
        monitor.check(_make_metrics(step=2, eval_pass_at_1=0.55, reward_variance=0.25))
        action = monitor.check(
            _make_metrics(step=3, eval_pass_at_1=0.4, reward_variance=0.3)
        )
        assert action is not None
        assert action.reason == KillReason.CATASTROPHIC_FORGETTING
        assert action.severity == "fatal"

    def test_dead_training(self, monitor: KillConditionMonitor):
        action = None
        for i in range(6):
            action = monitor.check(
                _make_metrics(
                    step=i, success_rate=0.0, reward_variance=0.1 + i * 0.05
                )
            )
        assert action is not None
        assert action.reason == KillReason.DEAD_TRAINING
        assert action.severity == "fatal"

    def test_report_oom(self, monitor: KillConditionMonitor):
        action = monitor.report_oom()
        assert action.reason == KillReason.OOM
        assert action.severity == "recoverable"

    def test_report_reward_hacking(self, monitor: KillConditionMonitor):
        action = monitor.report_reward_hacking("tests deleted from patch")
        assert action.reason == KillReason.REWARD_HACKING
        assert action.severity == "fatal"
        assert "tests deleted" in action.message
