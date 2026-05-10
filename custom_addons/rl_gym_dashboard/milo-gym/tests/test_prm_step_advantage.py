from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from src.core.config import PRMConfig
from src.prm.step_advantage import StepAdvantageEstimator, TurnSpan


class TestStepAdvantageEstimator:
    @pytest.fixture
    def rloo_estimator(self) -> StepAdvantageEstimator:
        return StepAdvantageEstimator(mode="rloo")

    @pytest.fixture
    def hybrid_estimator(self) -> StepAdvantageEstimator:
        return StepAdvantageEstimator(mode="hybrid", min_group_variance=0.01)

    @pytest.fixture
    def step_wise_estimator(self) -> StepAdvantageEstimator:
        return StepAdvantageEstimator(mode="step_wise", min_group_variance=0.01)

    def test_rloo_mode_returns_none(self, rloo_estimator):
        result = rloo_estimator.compute(
            step_rewards=[[0.1, 0.2], [0.3, 0.4]],
            turn_spans=[[TurnSpan(0, 0, 5), TurnSpan(1, 5, 10)]],
            seq_lengths=[10, 10],
            group_size=2,
        )
        assert result is None

    def test_rloo_should_not_override(self, rloo_estimator):
        assert not rloo_estimator.should_override_verl()

    def test_hybrid_should_override(self, hybrid_estimator):
        assert hybrid_estimator.should_override_verl()

    def test_hybrid_basic_shape(self, hybrid_estimator):
        step_rewards = [[0.1, 0.2], [0.3, 0.4]]
        turn_spans = [
            [TurnSpan(0, 0, 3), TurnSpan(1, 3, 6)],
            [TurnSpan(0, 0, 4), TurnSpan(1, 4, 8)],
        ]
        result = hybrid_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[6, 8],
            group_size=2,
        )
        assert result is not None
        assert result.shape == (2, 8)
        import torch
        returns = torch.tensor([0.3, 0.7])
        std = returns.std().item()
        expected_adv0 = (0.3 - 0.7) / max(std, 0.01)
        expected_adv1 = (0.7 - 0.3) / max(std, 0.01)
        assert abs(result[0, 0].item() - expected_adv0) < 1e-4
        assert abs(result[0, 5].item() - expected_adv0) < 1e-4
        assert abs(result[1, 0].item() - expected_adv1) < 1e-4

    def test_hybrid_skips_low_variance_group(self):
        estimator = StepAdvantageEstimator(mode="hybrid", min_group_variance=10.0)
        step_rewards = [[0.5], [0.5]]
        result = estimator.compute(
            step_rewards=step_rewards,
            turn_spans=[[TurnSpan(0, 0, 5)], [TurnSpan(0, 0, 5)]],
            seq_lengths=[5, 5],
            group_size=2,
        )
        assert result is not None
        assert torch.all(result == 0.0)

    def test_step_wise_broadcast_to_spans(self, step_wise_estimator):
        step_rewards = [[0.5, -0.3]]
        turn_spans = [[TurnSpan(0, 0, 4), TurnSpan(1, 4, 7)]]
        result = step_wise_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[7],
            group_size=1,
        )
        assert result is not None
        assert result[0, 0].item() == 0.5
        assert result[0, 3].item() == 0.5
        assert result[0, 4].item() == pytest.approx(-0.3, abs=1e-5)
        assert result[0, 6].item() == pytest.approx(-0.3, abs=1e-5)

    def test_step_wise_per_step_normalization(self, step_wise_estimator):
        step_rewards = [[0.2], [0.8]]
        turn_spans = [
            [TurnSpan(0, 0, 3)],
            [TurnSpan(0, 0, 3)],
        ]
        result = step_wise_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[3, 3],
            group_size=2,
        )
        assert result is not None
        assert abs(result[0, 0].item() - (-1.0)) < 1e-5
        assert abs(result[1, 0].item() - 1.0) < 1e-5

    def test_empty_batch(self, hybrid_estimator):
        result = hybrid_estimator.compute(
            step_rewards=[],
            turn_spans=[],
            seq_lengths=[],
            group_size=8,
        )
        assert result is not None
        assert result.shape == (0, 0)

    def test_from_config(self):
        config = PRMConfig(advantage_mode="step_wise", min_group_variance=0.05)
        estimator = StepAdvantageEstimator.from_config(config)
        assert estimator.mode == "step_wise"
        assert estimator.min_group_variance == 0.05


class TestGTPOAdvantage:
    @pytest.fixture
    def gtpo_estimator(self) -> StepAdvantageEstimator:
        return StepAdvantageEstimator(mode="gtpo", gamma=0.9)

    def test_gtpo_should_override_verl(self, gtpo_estimator):
        assert gtpo_estimator.should_override_verl()

    def test_gtpo_basic_discounted_returns(self, gtpo_estimator):
        step_rewards = [[0.1, 1.0]]
        turn_spans = [[TurnSpan(0, 0, 5), TurnSpan(1, 5, 10)]]
        result = gtpo_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[10],
            group_size=8,
        )
        assert result is not None
        R_0 = 0.1 + 0.9 * 1.0
        R_1 = 1.0
        mean_R = (R_0 + R_1) / 2.0
        var_R = ((R_0 - mean_R) ** 2 + (R_1 - mean_R) ** 2) / 2.0
        std_R = max(var_R**0.5, 1e-8)
        A_0 = (R_0 - mean_R) / std_R
        A_1 = (R_1 - mean_R) / std_R
        assert abs(result[0, 0].item() - A_0) < 1e-4
        assert abs(result[0, 4].item() - A_0) < 1e-4
        assert abs(result[0, 5].item() - A_1) < 1e-4
        assert abs(result[0, 9].item() - A_1) < 1e-4

    def test_gtpo_global_normalization_across_trajectories(self):
        estimator = StepAdvantageEstimator(mode="gtpo", gamma=0.9)
        step_rewards = [[0.5, 0.5], [1.0, 1.0]]
        turn_spans = [
            [TurnSpan(0, 0, 3), TurnSpan(1, 3, 6)],
            [TurnSpan(0, 0, 3), TurnSpan(1, 3, 6)],
        ]
        result = estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[6, 6],
            group_size=8,
        )
        assert result is not None
        assert result[0, 0].item() < result[1, 0].item()

    def test_gtpo_gamma_zero_equals_per_step(self):
        estimator = StepAdvantageEstimator(mode="gtpo", gamma=0.0)
        step_rewards = [[0.2, 0.8]]
        turn_spans = [[TurnSpan(0, 0, 5), TurnSpan(1, 5, 10)]]
        result = estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[10],
            group_size=8,
        )
        assert result is not None
        R_0 = 0.2
        R_1 = 0.8
        mean_R = (R_0 + R_1) / 2.0
        var_R = ((R_0 - mean_R) ** 2 + (R_1 - mean_R) ** 2) / 2.0
        std_R = var_R**0.5
        A_0 = (R_0 - mean_R) / std_R
        A_1 = (R_1 - mean_R) / std_R
        assert abs(result[0, 0].item() - A_0) < 1e-4
        assert abs(result[0, 5].item() - A_1) < 1e-4

    def test_gtpo_gamma_one_equals_cumulative(self):
        estimator = StepAdvantageEstimator(mode="gtpo", gamma=1.0)
        step_rewards = [[0.1, 0.2, 0.3]]
        turn_spans = [[TurnSpan(0, 0, 3), TurnSpan(1, 3, 6), TurnSpan(2, 6, 9)]]
        result = estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[9],
            group_size=8,
        )
        assert result is not None
        R_0 = 0.1 + 0.2 + 0.3
        R_1 = 0.2 + 0.3
        R_2 = 0.3
        all_R = [R_0, R_1, R_2]
        mean_R = sum(all_R) / 3.0
        var_R = sum((r - mean_R) ** 2 for r in all_R) / 3.0
        std_R = var_R**0.5
        A_0 = (R_0 - mean_R) / std_R
        assert abs(result[0, 0].item() - A_0) < 1e-4

    def test_gtpo_advantage_broadcast_to_all_tokens_in_span(self, gtpo_estimator):
        step_rewards = [[0.5, -0.3]]
        turn_spans = [[TurnSpan(0, 0, 8), TurnSpan(1, 8, 15)]]
        result = gtpo_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[15],
            group_size=8,
        )
        assert result is not None
        for t in range(8):
            assert result[0, t].item() == result[0, 0].item()
        for t in range(8, 15):
            assert result[0, t].item() == result[0, 8].item()
        assert result[0, 0].item() != result[0, 8].item()

    def test_gtpo_from_config(self):
        config = PRMConfig(advantage_mode="gtpo", min_group_variance=0.02, gtpo_gamma=0.95)
        estimator = StepAdvantageEstimator.from_config(config)
        assert estimator.mode == "gtpo"
        assert estimator.gamma == 0.95
        assert estimator.min_group_variance == 0.02
        assert estimator.should_override_verl()

    def test_gtpo_single_trajectory_normalizes(self, gtpo_estimator):
        step_rewards = [[0.1, 0.5, 1.0]]
        turn_spans = [[TurnSpan(0, 0, 3), TurnSpan(1, 3, 6), TurnSpan(2, 6, 9)]]
        result = gtpo_estimator.compute(
            step_rewards=step_rewards,
            turn_spans=turn_spans,
            seq_lengths=[9],
            group_size=8,
        )
        assert result is not None
        values = [result[0, 0].item(), result[0, 3].item(), result[0, 6].item()]
        mean_v = sum(values) / 3
        assert abs(mean_v) < 0.5
