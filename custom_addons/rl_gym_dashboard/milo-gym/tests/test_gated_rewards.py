from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from src.core.config import GatedRewardConfig
from src.training.gated_rewards import (
    GatedRewardComputer,
    OutcomeType,
    TrajectoryRewardInput,
)


class TestGateThreshold:
    @pytest.fixture
    def computer(self) -> GatedRewardComputer:
        return GatedRewardComputer(GatedRewardConfig(gate_threshold=0.0))

    def test_gate_closed_for_fail(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL,
                step_rewards=[0.5, 0.3, 0.2],
                episode_length=5,
                max_turns=50,
            )
        ]
        result = computer.compute_gated_rewards(inputs)
        outcome_fail = -0.1
        length_pen = computer.compute_length_penalty(5, 50)
        expected = outcome_fail + 0.1 * length_pen
        assert abs(result[0].item() - expected) < 1e-6

    def test_gate_open_for_pass(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.PASS,
                step_rewards=[0.5, 0.3],
                episode_length=5,
                max_turns=50,
            )
        ]
        result = computer.compute_gated_rewards(inputs)
        outcome_pass = 1.0
        step_sum = 0.8
        length_pen = computer.compute_length_penalty(5, 50)
        expected = outcome_pass + 0.05 * step_sum + 0.1 * length_pen
        assert abs(result[0].item() - expected) < 1e-6

    def test_gate_closed_for_timeout(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.TIMEOUT,
                step_rewards=[1.0, 1.0, 1.0],
                episode_length=50,
                max_turns=50,
            )
        ]
        result = computer.compute_gated_rewards(inputs)
        assert result[0].item() < 0

    def test_gate_closed_for_empty(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.EMPTY,
                step_rewards=[0.9],
                episode_length=3,
                max_turns=50,
            )
        ]
        result = computer.compute_gated_rewards(inputs)
        outcome_empty = -0.2
        length_pen = computer.compute_length_penalty(3, 50)
        expected = outcome_empty + 0.1 * length_pen
        assert abs(result[0].item() - expected) < 1e-6

    def test_batch_mixed_outcomes(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.PASS,
                step_rewards=[0.5],
                episode_length=3,
                max_turns=50,
            ),
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL,
                step_rewards=[0.5],
                episode_length=3,
                max_turns=50,
            ),
        ]
        result = computer.compute_gated_rewards(inputs)
        assert result[0].item() > result[1].item()
        assert result[1].item() < 0

    def test_fail_step_rewards_not_included(self, computer):
        large_step_rewards = [10.0, 10.0, 10.0]
        inputs_pass = [
            TrajectoryRewardInput(
                outcome=OutcomeType.PASS,
                step_rewards=large_step_rewards,
                episode_length=3,
                max_turns=50,
            )
        ]
        inputs_fail = [
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL,
                step_rewards=large_step_rewards,
                episode_length=3,
                max_turns=50,
            )
        ]
        pass_result = computer.compute_gated_rewards(inputs_pass)
        fail_result = computer.compute_gated_rewards(inputs_fail)
        assert pass_result[0].item() > 2.0
        assert fail_result[0].item() < 0


class TestPerTurnRewards:
    @pytest.fixture
    def computer(self) -> GatedRewardComputer:
        return GatedRewardComputer(GatedRewardConfig(gate_threshold=0.0))

    def test_pass_preserves_step_rewards(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.PASS,
                step_rewards=[0.1, 0.2, 0.3],
                episode_length=3,
                max_turns=50,
            )
        ]
        result = computer.compute_per_turn_rewards(inputs)
        assert len(result) == 1
        assert len(result[0]) == 3
        assert result[0][0] == pytest.approx(0.1, abs=1e-6)
        assert result[0][1] == pytest.approx(0.2, abs=1e-6)

    def test_fail_zeros_steps_adds_outcome(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL,
                step_rewards=[0.5, 0.3, 0.2],
                episode_length=3,
                max_turns=50,
            )
        ]
        result = computer.compute_per_turn_rewards(inputs)
        assert len(result[0]) == 3
        assert result[0][0] == pytest.approx(0.0, abs=1e-6)
        assert result[0][1] == pytest.approx(0.0, abs=1e-6)
        assert result[0][2] < 0

    def test_length_penalty_on_last_turn(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.PASS,
                step_rewards=[0.1, 0.2],
                episode_length=40,
                max_turns=50,
            )
        ]
        result = computer.compute_per_turn_rewards(inputs)
        length_pen = computer.compute_length_penalty(40, 50)
        outcome_pass = 1.0
        expected_last = 0.2 + outcome_pass + 0.1 * length_pen
        assert result[0][-1] == pytest.approx(expected_last, abs=1e-6)

    def test_empty_step_rewards(self, computer):
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL,
                step_rewards=[],
                episode_length=1,
                max_turns=50,
            )
        ]
        result = computer.compute_per_turn_rewards(inputs)
        assert len(result[0]) == 1
        assert result[0][0] < 0

    def test_output_length_matches_input(self, computer):
        for n_turns in [1, 5, 20, 50]:
            inputs = [
                TrajectoryRewardInput(
                    outcome=OutcomeType.PASS,
                    step_rewards=[0.1] * n_turns,
                    episode_length=n_turns,
                    max_turns=50,
                )
            ]
            result = computer.compute_per_turn_rewards(inputs)
            assert len(result[0]) == n_turns
