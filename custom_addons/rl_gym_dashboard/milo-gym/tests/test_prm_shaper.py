from __future__ import annotations

import pytest

from src.core.config import PRMConfig
from src.prm.shaper import PotentialShaper


class TestPotentialShaper:
    @pytest.fixture
    def shaper(self) -> PotentialShaper:
        return PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=True, gate_mode="multiply")

    @pytest.fixture
    def shaper_no_gate(self) -> PotentialShaper:
        return PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=False, gate_mode="multiply")

    @pytest.fixture
    def shaper_add_on_success(self) -> PotentialShaper:
        return PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=True, gate_mode="add_on_success")

    def test_empty_scores_returns_empty(self, shaper):
        assert shaper.shape([], 1.0) == []

    def test_single_turn_success(self, shaper):
        result = shaper.shape([0.5], 1.0)
        assert len(result) == 1
        assert abs(result[0] - 1.05) < 1e-9

    def test_single_turn_failure(self, shaper):
        result = shaper.shape([0.5], 0.0)
        assert len(result) == 1
        assert result[0] == 0.0

    def test_multi_turn_potential_differences(self, shaper_no_gate):
        scores = [None, 0.3, None, 0.7, None, 0.9]
        result = shaper_no_gate.shape(scores, 1.0)
        assert len(result) == 6
        assert abs(result[0] - 0.0) < 1e-9
        assert abs(result[1] - 0.03) < 1e-9
        assert abs(result[2] - 0.0) < 1e-9
        assert abs(result[3] - 0.04) < 1e-9
        assert abs(result[4] - 0.0) < 1e-9
        assert abs(result[5] - (0.02 + 1.0)) < 1e-9

    def test_outcome_gate_multiply_zeroes_process_on_failure(self, shaper):
        scores = [None, 0.5, None, 0.8]
        result = shaper.shape(scores, 0.0)
        assert all(r == 0.0 for r in result)

    def test_outcome_gate_add_on_success_zeroes_on_failure(self, shaper_add_on_success):
        scores = [None, 0.5, None, 0.8]
        result = shaper_add_on_success.shape(scores, 0.0)
        assert all(r == 0.0 for r in result)

    def test_outcome_gate_add_on_success_passes_on_success(self, shaper_add_on_success):
        scores = [0.5]
        result = shaper_add_on_success.shape(scores, 1.0)
        assert abs(result[0] - 1.05) < 1e-9

    def test_non_assistant_turns_dont_advance_potential(self, shaper_no_gate):
        scores = [None, 0.5, None, 0.5]
        result = shaper_no_gate.shape(scores, 1.0)
        assert abs(result[0] - 0.0) < 1e-9
        assert abs(result[1] - 0.05) < 1e-9
        assert abs(result[2] - 0.0) < 1e-9
        assert abs(result[3] - (0.0 + 1.0)) < 1e-9

    def test_shaped_return_equals_sum(self, shaper_no_gate):
        scores = [None, 0.3, None, 0.7]
        shaped = shaper_no_gate.shape(scores, 1.0)
        assert abs(shaper_no_gate.compute_shaped_return(shaped) - sum(shaped)) < 1e-9

    def test_invariant_shaped_return_formula(self, shaper_no_gate):
        scores = [0.2, 0.5, 0.8, 1.0]
        shaped = shaper_no_gate.shape(scores, 1.0)
        total = sum(shaped)
        expected = 0.1 * 1.0 + 1.0
        assert abs(total - expected) < 1e-9

    def test_from_config(self):
        config = PRMConfig(shaping_alpha=0.2, outcome_gate=False, gate_mode="add_on_success")
        shaper = PotentialShaper.from_config(config)
        assert shaper.alpha == 0.2
        assert shaper.outcome_gate is False
        assert shaper.gate_mode == "add_on_success"

    def test_alpha_zero_means_only_outcome(self, shaper_no_gate):
        shaper_no_gate.alpha = 0.0
        scores = [0.5, 0.8, 1.0]
        shaped = shaper_no_gate.shape(scores, 1.0)
        assert shaped[0] == 0.0
        assert shaped[1] == 0.0
        assert shaped[2] == 1.0

    def test_zero_score_is_valid_scored_turn(self, shaper_no_gate):
        """0.0 score for assistant = neutral (not non-scored). None = non-scored."""
        scores = [0.5, 0.0, 0.8]
        result = shaper_no_gate.shape(scores, 1.0)
        assert abs(result[0] - 0.05) < 1e-9   # 0.1*(0.5-0.0)
        assert abs(result[1] - (-0.05)) < 1e-9  # 0.1*(0.0-0.5)
        assert abs(result[2] - (0.08 + 1.0)) < 1e-9  # 0.1*(0.8-0.0) + outcome

    def test_negative_scores(self, shaper_no_gate):
        scores = [-0.5, 0.3, 0.8]
        result = shaper_no_gate.shape(scores, 1.0)
        assert abs(result[0] - (-0.05)) < 1e-9  # 0.1*(-0.5-0.0)
        assert abs(result[1] - 0.08) < 1e-9     # 0.1*(0.3-(-0.5))
        assert abs(result[2] - (0.05 + 1.0)) < 1e-9  # 0.1*(0.8-0.3) + outcome


class TestHardGateMode:
    @pytest.fixture
    def shaper_hard_gate(self) -> PotentialShaper:
        return PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=True, gate_mode="hard_gate")

    def test_hard_gate_zeros_on_failure(self, shaper_hard_gate):
        scores = [None, 0.5, None, 0.8]
        result = shaper_hard_gate.shape(scores, -0.1)
        # Bug #18 fix: shaping zeroed, but negative outcome still applied to last scored turn
        assert result[0] == 0.0
        assert result[1] == 0.0
        assert result[2] == 0.0
        assert abs(result[3] - (-0.1)) < 1e-9

    def test_hard_gate_zeros_on_zero_outcome(self, shaper_hard_gate):
        scores = [0.3, 0.7]
        result = shaper_hard_gate.shape(scores, 0.0)
        assert all(r == 0.0 for r in result)

    def test_hard_gate_passes_on_success(self, shaper_hard_gate):
        scores = [0.5]
        result = shaper_hard_gate.shape(scores, 1.0)
        assert abs(result[0] - 1.05) < 1e-9

    def test_hard_gate_equivalent_to_add_on_success(self):
        shaper_hard = PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=True, gate_mode="hard_gate")
        shaper_add = PotentialShaper(alpha=0.1, gamma=1.0, outcome_gate=True, gate_mode="add_on_success")
        scores = [None, 0.3, None, 0.7, None, 0.9]

        for outcome in [-1.0, -0.1, 0.0, 0.5, 1.0]:
            result_hard = shaper_hard.shape(scores, outcome)
            result_add = shaper_add.shape(scores, outcome)
            assert result_hard == result_add, f"Mismatch at outcome={outcome}"
