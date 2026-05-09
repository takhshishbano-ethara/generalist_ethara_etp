from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from src.core.config import GSPOConfig
from src.training.gtpo_loss import GTPOLossComputer


class TestGTPOLossComputer:
    @pytest.fixture
    def config(self) -> GSPOConfig:
        return GSPOConfig(clip_low=3e-4, clip_high=4e-4, norm_adv_by_std=False, beta_kl=0.0)

    @pytest.fixture
    def computer(self, config) -> GTPOLossComputer:
        return GTPOLossComputer(config)

    def test_positive_advantage_negative_loss(self, computer):
        batch, seq = 2, 10
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        assert result["loss"].item() < 0

    def test_zero_ratio_no_update(self, computer):
        batch, seq = 1, 5
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq) * 0.5
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        expected_loss = -0.5
        assert abs(result["loss"].item() - expected_loss) < 1e-5

    def test_clipping_behavior(self, computer):
        batch, seq = 1, 5
        log_probs = torch.ones(batch, seq) * 0.1
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        assert result["clip_frac"].item() > 0

    def test_dual_clip_negative_advantage(self, computer):
        batch, seq = 1, 5
        log_probs = torch.ones(batch, seq) * (-0.5)
        old_log_probs = torch.zeros(batch, seq)
        advantages = -torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        assert result["loss"].item() != 0

    def test_response_mask_respected(self, computer):
        batch, seq = 1, 10
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq) * 2.0
        response_mask = torch.zeros(batch, seq)
        response_mask[0, :5] = 1.0

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        expected_loss = -2.0
        assert abs(result["loss"].item() - expected_loss) < 1e-5

    def test_per_turn_advantage_constant(self, computer):
        batch, seq = 1, 20
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.zeros(batch, seq)
        advantages[0, :10] = 1.0
        advantages[0, 10:] = -0.5
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        expected_loss = -(1.0 * 10 + (-0.5) * 10) / 20
        assert abs(result["loss"].item() - expected_loss) < 1e-5

    def test_gradient_flows(self, computer):
        batch, seq = 2, 8
        log_probs = torch.randn(batch, seq, requires_grad=True)
        old_log_probs = torch.randn(batch, seq)
        advantages = torch.randn(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        result["loss"].backward()
        assert log_probs.grad is not None
        assert log_probs.grad.abs().sum().item() > 0

    def test_kl_penalty_when_enabled(self):
        config = GSPOConfig(clip_low=3e-4, clip_high=4e-4, norm_adv_by_std=False, beta_kl=0.1)
        computer = GTPOLossComputer(config)

        batch, seq = 1, 5
        log_probs = torch.ones(batch, seq) * 0.01
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        assert result["kl_penalty"].item() > 0

    def test_norm_adv_by_std(self):
        config = GSPOConfig(clip_low=3e-4, clip_high=4e-4, norm_adv_by_std=True, beta_kl=0.0)
        computer = GTPOLossComputer(config)

        batch, seq = 2, 5
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.tensor([[1.0] * 5, [3.0] * 5])
        response_mask = torch.ones(batch, seq)

        result_normed = computer(log_probs, old_log_probs, advantages, response_mask)

        config_no_norm = GSPOConfig(clip_low=3e-4, clip_high=4e-4, norm_adv_by_std=False, beta_kl=0.0)
        computer_no_norm = GTPOLossComputer(config_no_norm)
        result_raw = computer_no_norm(log_probs, old_log_probs, advantages, response_mask)

        assert result_normed["loss"].item() != result_raw["loss"].item()

    def test_same_interface_as_gspo(self, config):
        from src.training.gspo_loss import GSPOLossComputer as GSPOComp

        gtpo = GTPOLossComputer(config)
        gspo = GSPOComp(config)

        batch, seq = 2, 10
        log_probs = torch.zeros(batch, seq)
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)
        segment_ids = torch.ones(batch, seq, dtype=torch.long)

        gtpo_result = gtpo(log_probs, old_log_probs, advantages, response_mask, segment_ids)
        assert "loss" in gtpo_result
        assert "clip_frac" in gtpo_result
        assert "approx_kl" in gtpo_result
        assert "mean_ratio" in gtpo_result
        assert "kl_penalty" in gtpo_result

    def test_asymmetric_clip(self):
        config = GSPOConfig(clip_low=1e-3, clip_high=5e-3, norm_adv_by_std=False, beta_kl=0.0)
        computer = GTPOLossComputer(config)

        batch, seq = 1, 5
        log_probs = torch.ones(batch, seq) * 0.01
        old_log_probs = torch.zeros(batch, seq)
        advantages = torch.ones(batch, seq)
        response_mask = torch.ones(batch, seq)

        result = computer(log_probs, old_log_probs, advantages, response_mask)
        assert result["clip_frac"].item() > 0
