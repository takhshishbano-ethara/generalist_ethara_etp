from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.schemas import RewardResult, TaskSpec, TrainingMetrics, Trajectory, Turn


def test_turn_valid_roles():
    for role in ("user", "assistant", "tool", "system"):
        turn = Turn(role=role, content="hello")
        assert turn.role == role


def test_turn_invalid_role():
    with pytest.raises(ValidationError):
        Turn(role="invalid", content="hello")


def test_trajectory_is_success(sample_trajectory: Trajectory):
    assert sample_trajectory.is_success is True


def test_trajectory_is_not_success(failed_trajectory: Trajectory):
    assert failed_trajectory.is_success is False


def test_trajectory_total_tokens(sample_trajectory: Trajectory):
    assert sample_trajectory.total_tokens == 60


def test_task_spec_empty_id():
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="",
            repo="org/repo",
            language="python",
            base_commit="abc",
            problem_statement="fix",
            test_patch="patch",
            fix_patch="patch",
            docker_image="img:latest",
            evaluation_script="echo ok",
        )


def test_task_spec_difficulty_score_above_bounds():
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="valid_id",
            repo="org/repo",
            language="python",
            base_commit="abc",
            problem_statement="fix",
            test_patch="patch",
            fix_patch="patch",
            docker_image="img:latest",
            evaluation_script="echo ok",
            difficulty_score=1.5,
        )


def test_task_spec_difficulty_score_below_bounds():
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="valid_id",
            repo="org/repo",
            language="python",
            base_commit="abc",
            problem_statement="fix",
            test_patch="patch",
            fix_patch="patch",
            docker_image="img:latest",
            evaluation_script="echo ok",
            difficulty_score=-0.1,
        )


def test_reward_result_f2p_pass():
    assert RewardResult(reward=1.0, f2p_passed=3, f2p_total=3).f2p_pass is True
    assert RewardResult(reward=0.5, f2p_passed=2, f2p_total=3).f2p_pass is False
    assert RewardResult(reward=0.0, f2p_passed=0, f2p_total=0).f2p_pass is False


def test_reward_result_p2p_pass():
    assert RewardResult(reward=1.0, p2p_passed=0, p2p_total=0).p2p_pass is True
    assert RewardResult(reward=1.0, p2p_passed=5, p2p_total=5).p2p_pass is True
    assert RewardResult(reward=0.5, p2p_passed=3, p2p_total=5).p2p_pass is False


def test_training_metrics_creation():
    m = TrainingMetrics(
        step=100,
        success_rate=0.45,
        mask_rate=0.1,
        avg_episode_length=12.5,
        reward_variance=0.3,
        grad_norm=1.2,
        learning_rate=3e-5,
        unique_tasks_solved=42,
        curriculum_phase=2,
        total_rollouts=500,
    )
    assert m.step == 100
    assert m.curriculum_phase == 2
    assert m.eval_pass_at_1 is None
