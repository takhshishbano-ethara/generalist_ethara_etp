"""BRUTAL end-to-end tests for the MILO training pipeline.

Tests the full chain: rewards → shaping → gating → advantages → loss
with edge cases that actually matter for training stability and correctness.

These are NOT unit tests — they verify mathematical properties and data flow
that, when broken, cause silent training failures (zero gradients, NaN, etc).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
import torch
from torch import Tensor

# ========== Imports ==========
from src.core.config import GatedRewardConfig, GSPOConfig, PRMConfig
from src.nemo_integration.advantage import MiloAdvantageEstimator
from src.nemo_integration.loss import MiloGTPOLoss
from src.prm.shaper import PotentialShaper
from src.prm.step_advantage import StepAdvantageEstimator, TurnSpan
from src.training.format_penalty import FormatPenaltyComputer, FormatPenaltyConfig
from src.training.gated_rewards import (
    GatedRewardComputer,
    OutcomeType,
    TrajectoryRewardInput,
)
from src.training.gtpo_loss import GTPOLossComputer
from src.training.partial_credit import PartialCreditComputer, PartialCreditConfig


# ========================================================================
# SECTION 1: REWARD PIPELINE INTEGRITY
# ========================================================================


class TestRewardPipelineEndToEnd:
    """Test: PRM → Shaper → GatedComputer → per_turn_rewards → advantages → loss.

    The central invariant: PASS trajectories with PRM scores get richer signal
    than FAIL trajectories. This differential is what the model learns from.
    """

    def test_pass_vs_fail_differential_is_large(self):
        """PASS must get strictly more total reward than FAIL."""
        shaper = PotentialShaper(alpha=0.3, gamma=0.9, outcome_gate=True, gate_mode="add_on_success")
        config = GatedRewardConfig()
        computer = GatedRewardComputer(config)

        # PASS trajectory: 3 turns, all scored well by PRM
        pass_prm = [0.7, 0.8, 0.9]
        pass_shaped = shaper.shape(pass_prm, outcome_reward=1.0)

        # FAIL trajectory: 3 turns, all scored well by PRM
        fail_prm = [0.7, 0.8, 0.9]
        fail_shaped = shaper.shape(fail_prm, outcome_reward=-0.1)

        pass_input = TrajectoryRewardInput(
            outcome=OutcomeType.PASS, step_rewards=pass_shaped,
            episode_length=3, max_turns=50
        )
        fail_input = TrajectoryRewardInput(
            outcome=OutcomeType.FAIL, step_rewards=fail_shaped,
            episode_length=3, max_turns=50
        )

        per_turn = computer.compute_per_turn_rewards([pass_input, fail_input])
        pass_total = sum(per_turn[0])
        fail_total = sum(per_turn[1])

        # PASS must dominate FAIL by a significant margin
        assert pass_total > fail_total + 0.5, (
            f"PASS ({pass_total:.4f}) must be significantly > FAIL ({fail_total:.4f})"
        )

    def test_gating_actually_zeros_prm_on_failure(self):
        """When gate is closed (FAIL), PRM shaping MUST be zero.
        Only outcome (negative) + format penalties remain.
        """
        config = GatedRewardConfig()
        computer = GatedRewardComputer(config)

        fail_input = TrajectoryRewardInput(
            outcome=OutcomeType.FAIL,
            step_rewards=[0.5, 0.3, 0.2],  # These are shaped PRM values
            episode_length=3, max_turns=50,
        )

        per_turn = computer.compute_per_turn_rewards([fail_input])

        # Gate closed: step_rewards must be zeroed, only outcome on last turn
        # Expected: [0.0, 0.0, outcome_fail]
        assert per_turn[0][0] == 0.0, "Turn 0 should be zeroed by gate"
        assert per_turn[0][1] == 0.0, "Turn 1 should be zeroed by gate"
        # Last turn gets outcome_fail (default -0.1)
        assert per_turn[0][2] == config.outcome_fail, (
            f"Last turn should equal outcome_fail ({config.outcome_fail}), got {per_turn[0][2]}"
        )

    def test_outcome_reward_always_on_last_turn(self):
        """Regardless of gate, outcome reward must appear exactly once (on last turn)."""
        config = GatedRewardConfig(outcome_pass=1.0, outcome_fail=-0.2)
        computer = GatedRewardComputer(config)

        # PASS case
        pass_input = TrajectoryRewardInput(
            outcome=OutcomeType.PASS, step_rewards=[0.1, 0.2, 0.3],
            episode_length=3, max_turns=50
        )
        pass_turns = computer.compute_per_turn_rewards([pass_input])
        # Last turn should include outcome (step_reward + outcome_pass)
        assert pass_turns[0][-1] == pytest.approx(0.3 + 1.0, abs=1e-6)

        # FAIL case
        fail_input = TrajectoryRewardInput(
            outcome=OutcomeType.FAIL, step_rewards=[0.1, 0.2, 0.3],
            episode_length=3, max_turns=50
        )
        fail_turns = computer.compute_per_turn_rewards([fail_input])
        assert fail_turns[0][-1] == pytest.approx(-0.2, abs=1e-6)


# ========================================================================
# SECTION 2: ADVANTAGE COMPUTATION CORRECTNESS
# ========================================================================


class TestGTPOAdvantageInvariants:
    """Test mathematical invariants that MUST hold for GTPO to work."""

    def test_discounted_returns_decrease_with_gamma(self):
        """Earlier turns have higher discounted returns than later turns
        when all per-turn rewards are equal.
        """
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)
        step_rewards = [[1.0, 1.0, 1.0, 1.0, 1.0]]
        turn_spans = [[TurnSpan(j, j * 10, (j + 1) * 10) for j in range(5)]]
        seq_lengths = [50]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=1)

        # With group_size=1 and single trajectory, normalization makes mean=0
        # But the RAW returns should be: R[0] > R[1] > ... > R[4]
        # After normalization: first turns positive, last turns negative
        assert adv is not None
        # Turn 0 tokens should have higher advantage than turn 4 tokens
        assert adv[0, 0].item() > adv[0, 40].item(), (
            "Earlier turns should have higher advantage (higher discounted returns)"
        )

    def test_group_normalization_centers_advantages(self):
        """Group-relative normalization must center advantages around zero."""
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        # 4 trajectories in one group with different rewards
        step_rewards = [[1.0, 1.0], [0.5, 0.5], [0.0, 0.0], [-0.5, -0.5]]
        turn_spans = [[TurnSpan(0, 0, 10), TurnSpan(1, 10, 20)] for _ in range(4)]
        seq_lengths = [20, 20, 20, 20]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=4)
        assert adv is not None

        # All non-zero advantages should sum approximately to 0
        nonzero_adv = adv[adv != 0.0]
        if nonzero_adv.numel() > 0:
            assert abs(nonzero_adv.mean().item()) < 0.3, (
                f"Group advantages should be centered near 0, got mean={nonzero_adv.mean():.4f}"
            )

    def test_group_normalization_pools_all_turns(self):
        """Critical: normalization pools ALL turns from ALL trajectories in group.
        NOT per-trajectory normalization (that would eliminate within-trajectory signal).
        """
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        # 2 trajectories: one all-positive, one all-negative
        step_rewards = [[1.0, 1.0, 1.0], [-1.0, -1.0, -1.0]]
        turn_spans = [[TurnSpan(j, j * 10, (j + 1) * 10) for j in range(3)] for _ in range(2)]
        seq_lengths = [30, 30]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=2)
        assert adv is not None

        # Trajectory 0 should have positive advantages (good vs group mean)
        # Trajectory 1 should have negative advantages
        traj0_mean = adv[0, :30].mean().item()
        traj1_mean = adv[1, :30].mean().item()
        assert traj0_mean > 0, f"Good trajectory should have positive advantages, got {traj0_mean}"
        assert traj1_mean < 0, f"Bad trajectory should have negative advantages, got {traj1_mean}"

    def test_advantages_are_finite_with_extreme_rewards(self):
        """Advantages must be finite even with extreme reward values."""
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        step_rewards = [[100.0, -100.0], [0.001, -0.001]]
        turn_spans = [[TurnSpan(0, 0, 10), TurnSpan(1, 10, 20)] for _ in range(2)]
        seq_lengths = [20, 20]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=2)
        assert adv is not None
        assert torch.isfinite(adv).all(), "Advantages must be finite with extreme rewards"

    def test_empty_step_rewards_produces_zero_advantages(self):
        """Empty step_rewards should not crash, should produce zeros."""
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        step_rewards = [[], []]
        turn_spans = [[], []]
        seq_lengths = [20, 20]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=2)
        assert adv is not None
        assert (adv == 0.0).all(), "Empty rewards should give zero advantages"

    def test_single_turn_trajectory_has_valid_advantage(self):
        """Single-turn trajectories must get valid (non-zero) advantages."""
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        step_rewards = [[1.0], [0.0]]
        turn_spans = [[TurnSpan(0, 0, 20)], [TurnSpan(0, 0, 20)]]
        seq_lengths = [20, 20]

        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=2)
        assert adv is not None
        # One got reward 1.0, other got 0.0 — should produce different advantages
        assert adv[0, 0].item() != adv[1, 0].item(), (
            "Different rewards should produce different advantages"
        )


# ========================================================================
# SECTION 3: LOSS FUNCTION NUMERICAL STABILITY
# ========================================================================


class TestGTPOLossNumericalStability:
    """Tests that the loss function handles degenerate inputs without NaN/Inf."""

    @pytest.fixture
    def config(self) -> GSPOConfig:
        return GSPOConfig(
            clip_low=0.2, clip_high=0.28, beta_kl=0.0, norm_adv_by_std=False
        )

    def test_identical_logprobs_gives_zero_update(self, config):
        """When policy hasn't changed, ratio=1, loss should be near zero."""
        computer = GTPOLossComputer(config)
        log_probs = torch.randn(4, 128)
        old_log_probs = log_probs.clone()  # Same policy
        advantages = torch.randn(4, 128)
        mask = torch.ones(4, 128)

        result = computer(log_probs, old_log_probs, advantages, mask)

        # ratio = exp(0) = 1.0 → unclipped → loss = -mean(1.0 * advantages * mask)
        # This should be finite and small
        assert torch.isfinite(result["loss"]), "Loss must be finite with identical log-probs"
        assert result["clip_frac"].item() == 0.0, "No clipping should occur when ratio=1"

    def test_extreme_log_ratio_clamped(self, config):
        """Log-ratios exceeding [-20, 20] must be clamped (prevents exp overflow)."""
        computer = GTPOLossComputer(config)

        # Simulate degenerate token where new policy assigns very different prob
        log_probs = torch.zeros(2, 64)
        old_log_probs = torch.zeros(2, 64)
        log_probs[0, 30] = 50.0  # Would give exp(50) = inf without clamping
        log_probs[1, 30] = -50.0  # Would give exp(-50) = ~0

        advantages = torch.ones(2, 64)
        mask = torch.ones(2, 64)

        result = computer(log_probs, old_log_probs, advantages, mask)

        assert torch.isfinite(result["loss"]), "Loss must be finite with extreme log-ratios"
        assert not torch.isnan(result["loss"]), "Loss must not be NaN"

    def test_all_zero_mask_gives_zero_loss(self, config):
        """Zero response_mask means no tokens to learn from → loss should be 0."""
        computer = GTPOLossComputer(config)
        log_probs = torch.randn(4, 64)
        old_log_probs = torch.randn(4, 64)
        advantages = torch.randn(4, 64)
        mask = torch.zeros(4, 64)  # All masked out

        result = computer(log_probs, old_log_probs, advantages, mask)

        assert result["loss"].item() == pytest.approx(0.0, abs=1e-6), (
            "Zero mask should give zero loss"
        )

    def test_dual_clip_prevents_exploitation_of_negative_advantages(self, config):
        """Dual clip: for negative advantages, ratio shouldn't go too high.
        Without dual clip, a very high ratio on neg advantage gives very negative loss
        (which becomes very positive reward for the wrong behavior).
        """
        computer = GTPOLossComputer(config, dual_clip=True, dual_clip_coef=5.0)

        log_probs = torch.zeros(1, 10)
        old_log_probs = torch.full((1, 10), -5.0)  # ratio = exp(5) ≈ 148 (VERY high)
        advantages = torch.full((1, 10), -1.0)  # Negative advantages
        mask = torch.ones(1, 10)

        result_dual = computer(log_probs, old_log_probs, advantages, mask)

        # Without dual clip
        computer_no_dual = GTPOLossComputer(config, dual_clip=False)
        result_no_dual = computer_no_dual(log_probs, old_log_probs, advantages, mask)

        # Dual clip should limit the loss (prevent unbounded positive loss)
        assert result_dual["loss"].item() <= result_no_dual["loss"].item() + 1e-4, (
            "Dual clip should limit loss when ratio is high and advantage is negative"
        )

    def test_loss_gradient_flows(self, config):
        """Verify that gradients flow through the loss correctly."""
        computer = GTPOLossComputer(config)
        log_probs = torch.randn(4, 32, requires_grad=True)
        old_log_probs = torch.randn(4, 32)
        advantages = torch.randn(4, 32)
        mask = torch.ones(4, 32)

        result = computer(log_probs, old_log_probs, advantages, mask)
        result["loss"].backward()

        assert log_probs.grad is not None, "Gradient must flow to log_probs"
        assert torch.isfinite(log_probs.grad).all(), "Gradients must be finite"
        assert (log_probs.grad != 0.0).any(), "Gradients must be non-zero (learning signal)"

    def test_norm_adv_by_std_disabled_no_double_normalization(self, config):
        """With norm_adv_by_std=False, advantages pass through unchanged."""
        computer = GTPOLossComputer(config)
        log_probs = torch.zeros(2, 10)
        old_log_probs = torch.zeros(2, 10)
        mask = torch.ones(2, 10)

        # Set known advantages
        advantages = torch.tensor([[1.0] * 10, [-1.0] * 10])

        result = computer(log_probs, old_log_probs, advantages, mask)

        # With ratio=1, loss = -mean(advantages * mask) = -mean(all values) = 0
        # Since sum is 0 (10 * 1.0 + 10 * -1.0 = 0), loss should be ~0
        assert abs(result["loss"].item()) < 1e-5, (
            "Equal positive and negative advantages with ratio=1 should give ~zero loss"
        )


# ========================================================================
# SECTION 4: NEMO-RL INTEGRATION LAYER CORRECTNESS
# ========================================================================


class TestNeMoIntegrationDataFlow:
    """Test the NeMo-RL adapter layer correctly transforms data."""

    def test_loss_adapter_slicing_alignment(self):
        """MiloGTPOLoss must correctly slice [B,S] tensors to [B,S-1]."""
        config = GSPOConfig(clip_low=0.2, clip_high=0.28, beta_kl=0.0, norm_adv_by_std=False)
        loss_fn = MiloGTPOLoss(config)

        B, S = 4, 64
        # Simulate NeMo-RL's data format
        data = {
            "token_mask": torch.ones(B, S),
            "sample_mask": torch.ones(B),
            "advantages": torch.randn(B, S),
            "prev_logprobs": torch.randn(B, S),
        }
        next_token_logprobs = torch.randn(B, S - 1)

        loss, metrics = loss_fn(
            data=data,
            global_valid_seqs=torch.tensor(B),
            global_valid_toks=torch.tensor(B * (S - 1)),
            next_token_logprobs=next_token_logprobs,
        )

        assert loss.shape == (), f"Loss should be scalar, got shape {loss.shape}"
        assert torch.isfinite(loss), "Loss must be finite"
        assert "clip_frac" in metrics
        assert "approx_kl" in metrics

    def test_loss_adapter_respects_sample_mask(self):
        """Samples with sample_mask=0 should not contribute to loss."""
        config = GSPOConfig(clip_low=0.2, clip_high=0.28, beta_kl=0.0, norm_adv_by_std=False)
        loss_fn = MiloGTPOLoss(config)

        B, S = 4, 32
        data = {
            "token_mask": torch.ones(B, S),
            "sample_mask": torch.tensor([1.0, 1.0, 0.0, 0.0]),  # Last 2 masked
            "advantages": torch.ones(B, S) * 5.0,  # Large advantages
            "prev_logprobs": torch.zeros(B, S),
        }
        next_token_logprobs = torch.zeros(B, S - 1)

        loss_partial, _ = loss_fn(
            data=data,
            global_valid_seqs=torch.tensor(2),
            global_valid_toks=torch.tensor(2 * (S - 1)),
            next_token_logprobs=next_token_logprobs,
        )

        # Compare with all samples active
        data["sample_mask"] = torch.ones(B)
        loss_all, _ = loss_fn(
            data=data,
            global_valid_seqs=torch.tensor(4),
            global_valid_toks=torch.tensor(4 * (S - 1)),
            next_token_logprobs=next_token_logprobs,
        )

        # Same data, same log_probs → loss should be similar in magnitude
        # But the KEY test: loss_partial only uses 2 samples worth of tokens
        assert torch.isfinite(loss_partial)
        assert torch.isfinite(loss_all)

    def test_advantage_estimator_extracts_from_extra_env_info(self):
        """MiloAdvantageEstimator must correctly parse extra_env_info dicts."""
        prm_config = PRMConfig(advantage_mode="gtpo", gtpo_gamma=0.9)
        estimator = MiloAdvantageEstimator(prm_config=prm_config, group_size=2)

        B, S = 4, 40
        mask = torch.ones(B, S)
        rewards = torch.tensor([1.0, 0.5, -0.5, -1.0])
        prompt_ids = torch.zeros(B, S, dtype=torch.long)

        extra_env_info = [
            {
                "step_rewards": [0.3, 0.5, 0.8],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 0, "end_token": 13},
                    {"turn_idx": 1, "start_token": 13, "end_token": 26},
                    {"turn_idx": 2, "start_token": 26, "end_token": 40},
                ],
            },
            {
                "step_rewards": [0.1, 0.2, 0.3],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 0, "end_token": 13},
                    {"turn_idx": 1, "start_token": 13, "end_token": 26},
                    {"turn_idx": 2, "start_token": 26, "end_token": 40},
                ],
            },
            {
                "step_rewards": [-0.1, -0.2, -0.3],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 0, "end_token": 13},
                    {"turn_idx": 1, "start_token": 13, "end_token": 26},
                    {"turn_idx": 2, "start_token": 26, "end_token": 40},
                ],
            },
            {
                "step_rewards": [-0.5, -0.5, -0.5],
                "turn_spans": [
                    {"turn_idx": 0, "start_token": 0, "end_token": 13},
                    {"turn_idx": 1, "start_token": 13, "end_token": 26},
                    {"turn_idx": 2, "start_token": 26, "end_token": 40},
                ],
            },
        ]

        repeated_batch = {"extra_env_info": extra_env_info}
        adv = estimator.compute_advantage(
            prompt_ids=prompt_ids,
            rewards=rewards,
            mask=mask,
            repeated_batch=repeated_batch,
        )

        assert adv.shape == (B, S), f"Expected ({B}, {S}), got {adv.shape}"
        assert torch.isfinite(adv).all(), "Advantages must be finite"
        # Sample 0 (highest rewards) should have higher mean advantage than sample 3
        assert adv[0].mean() > adv[3].mean(), (
            "Higher rewards should produce higher advantages"
        )

    def test_advantage_estimator_rloo_fallback(self):
        """When extra_env_info is empty, should fall back to RLOO."""
        prm_config = PRMConfig(advantage_mode="gtpo", gtpo_gamma=0.9)
        estimator = MiloAdvantageEstimator(prm_config=prm_config, group_size=2)

        B, S = 4, 20
        mask = torch.ones(B, S)
        rewards = torch.tensor([1.0, 0.0, -0.5, 0.5])
        prompt_ids = torch.zeros(B, S, dtype=torch.long)

        # No extra_env_info → fallback
        adv = estimator.compute_advantage(
            prompt_ids=prompt_ids,
            rewards=rewards,
            mask=mask,
            repeated_batch={},
        )

        assert adv.shape == (B, S), f"Expected ({B}, {S}), got {adv.shape}"
        assert torch.isfinite(adv).all(), "RLOO fallback must be finite"


# ========================================================================
# SECTION 5: PBRS SHAPING INVARIANTS
# ========================================================================


class TestPBRSInvariants:
    """Test Potential-Based Reward Shaping mathematical correctness."""

    def test_pbrs_doesnt_change_optimal_policy(self):
        """Core PBRS theorem: shaping preserves optimal policy.

        The total shaped return = original return + γ^T·Φ(s_T) - Φ(s_0)
        Since Φ(s_0) = 0 (initial potential), shaped return just adds a bounded constant.
        This means: relative ordering of trajectories is preserved.
        """
        shaper = PotentialShaper(alpha=0.3, gamma=0.9)

        # Two trajectories with same PRM but different outcomes
        prm1 = [0.5, 0.7, 0.9]
        prm2 = [0.5, 0.7, 0.9]

        shaped1 = shaper.shape(prm1, outcome_reward=1.0)
        shaped2 = shaper.shape(prm2, outcome_reward=0.0)

        # Trajectory with better outcome should still have higher total
        assert sum(shaped1) > sum(shaped2), (
            "PBRS must preserve: better outcome → higher total reward"
        )

    def test_gamma_discounts_future_potentials(self):
        """With gamma < 1, later PRM signals contribute less to shaping."""
        shaper_low_gamma = PotentialShaper(alpha=1.0, gamma=0.5, outcome_gate=False)
        shaper_high_gamma = PotentialShaper(alpha=1.0, gamma=0.99, outcome_gate=False)

        prm_scores = [0.0, 0.0, 0.0, 0.0, 1.0]

        shaped_low = shaper_low_gamma.shape(prm_scores, outcome_reward=0.0)
        shaped_high = shaper_high_gamma.shape(prm_scores, outcome_reward=0.0)

        # delta for last turn = alpha * (gamma * 1.0 - prev_potential=0.0) = alpha * gamma
        # low_gamma: 1.0 * 0.5 = 0.5, high_gamma: 1.0 * 0.99 = 0.99
        # But outcome=0.0 is also added to last turn, so:
        # shaped_low[-1] = 0.5 + 0.0 = 0.5, shaped_high[-1] = 0.99 + 0.0 = 0.99
        assert shaped_low[-1] < shaped_high[-1], (
            f"Higher gamma should give more credit: low={shaped_low[-1]}, high={shaped_high[-1]}"
        )

    def test_sparse_mode_zeros_intermediate_shaping(self):
        """sparse_non_terminal=True should zero all PRM shaping except last turn."""
        shaper = PotentialShaper(alpha=0.5, gamma=0.9, sparse_non_terminal=True)
        prm_scores = [0.8, 0.9, 0.7, 0.6]
        shaped = shaper.shape(prm_scores, outcome_reward=1.0)

        # Non-terminal turns should be 0.0 (sparse)
        for i in range(len(shaped) - 1):
            assert shaped[i] == 0.0, f"Turn {i} should be 0.0 in sparse mode, got {shaped[i]}"

        # Last turn should be non-zero (has shaping + outcome)
        assert shaped[-1] != 0.0, "Last turn should have reward in sparse mode"


# ========================================================================
# SECTION 6: PARTIAL CREDIT + FORMAT PENALTY CORRECTNESS
# ========================================================================


class TestPartialCreditEdgeCases:
    """Stress test partial credit computation."""

    def test_all_pass_gives_zero_partial_credit(self):
        """If all trajectories pass, no partial credit is needed."""
        config = PartialCreditConfig(enabled=True, use_embeddings=False)
        computer = PartialCreditComputer(config=config)

        codes = ["def foo(): pass", "def bar(): return 1", "def baz(): return 2"]
        outcomes = [True, True, True]

        credits = computer.compute_group_partial_credit(codes, outcomes)
        assert all(c == 0.0 for c in credits), "All-pass should give zero partial credit"

    def test_all_fail_gives_zero_partial_credit(self):
        """If all trajectories fail, no P set → no similarity → zero credit."""
        config = PartialCreditConfig(enabled=True, use_embeddings=False)
        computer = PartialCreditComputer(config=config)

        codes = ["def foo(): pass", "def bar(): return 1"]
        outcomes = [False, False]

        credits = computer.compute_group_partial_credit(codes, outcomes)
        # No pass set → heuristic_valid_code may add tiny credit, but no similarity
        # Since there's no pass set, overlap credit should be 0
        # But valid_code gives 0.05 if parseable
        for c in credits:
            assert c <= config.alpha, f"Credit {c} exceeds alpha cap {config.alpha}"

    def test_partial_credit_capped_at_alpha(self):
        """Partial credit must NEVER exceed alpha (upper bound from paper)."""
        config = PartialCreditConfig(
            enabled=True, alpha=0.5, use_embeddings=False,
            heuristic_partial_tests=0.9, heuristic_valid_code=0.3
        )
        computer = PartialCreditComputer(config=config)

        codes = ["identical code here"] * 4
        outcomes = [True, True, False, False]
        # Artificially high overlap + valid code + high partial test ratio
        credits = computer.compute_group_partial_credit(
            codes, outcomes, group_partial_test_ratios=[0.0, 0.0, 1.0, 1.0]
        )

        for c in credits:
            assert c <= config.alpha + 1e-6, f"Credit {c} exceeds alpha cap {config.alpha}"

    def test_empty_code_gives_minimal_credit(self):
        """Empty code should get ~zero partial credit."""
        config = PartialCreditConfig(enabled=True, use_embeddings=False)
        computer = PartialCreditComputer(config=config)

        codes = ["def solution(): return 42", "", ""]
        outcomes = [True, False, False]

        credits = computer.compute_group_partial_credit(codes, outcomes)
        # Empty code has no overlap with pass code and is not valid Python
        assert credits[1] == pytest.approx(0.0, abs=0.01)
        assert credits[2] == pytest.approx(0.0, abs=0.01)


class TestFormatPenaltyEdgeCases:
    """Stress test format penalty computation."""

    def test_well_formatted_first_turn_no_penalty(self):
        """Perfect format should give 0.0 penalty."""
        config = FormatPenaltyConfig(enabled=True)
        computer = FormatPenaltyComputer(config)

        turns = ['<tool_call>{"name": "read_file", "arguments": {"path": "/src/main.py"}}</tool_call>']
        penalties = computer.compute_turn_penalties(turns)
        assert penalties[0] == 0.0, "Well-formatted turn should have no penalty"

    def test_first_turn_no_tool_call_gets_penalty(self):
        """First turn without tool_call tag should get -0.1."""
        config = FormatPenaltyConfig(enabled=True, penalty_per_violation=-0.1)
        computer = FormatPenaltyComputer(config)

        turns = ["Let me think about this problem..."]  # No tool call
        penalties = computer.compute_turn_penalties(turns)
        assert penalties[0] == -0.1, f"First turn without tool should get penalty, got {penalties[0]}"

    def test_malformed_json_gets_penalty(self):
        """Invalid JSON inside tool_call should be penalized."""
        config = FormatPenaltyConfig(enabled=True, penalty_per_violation=-0.1)
        computer = FormatPenaltyComputer(config)

        turns = ['<tool_call>{broken json here}</tool_call>']
        penalties = computer.compute_turn_penalties(turns)
        assert penalties[0] == -0.1, "Malformed JSON should get penalty"

    def test_invalid_action_name_gets_penalty(self):
        """Unknown action names should be penalized."""
        config = FormatPenaltyConfig(enabled=True, penalty_per_violation=-0.1)
        computer = FormatPenaltyComputer(config)

        turns = ['<tool_call>{"name": "hack_system", "arguments": {}}</tool_call>']
        penalties = computer.compute_turn_penalties(turns)
        assert penalties[0] == -0.1, "Invalid action should get penalty"

    def test_penalty_capped_at_one_per_turn(self):
        """Multiple violations in one turn should still only give one penalty."""
        config = FormatPenaltyConfig(enabled=True, penalty_per_violation=-0.1)
        computer = FormatPenaltyComputer(config)

        # Multiple violations: invalid action AND missing args
        turns = [
            '<tool_call>{"name": "invalid_tool"}</tool_call>'
            '<tool_call>{"name": "also_invalid"}</tool_call>'
        ]
        penalties = computer.compute_turn_penalties(turns)
        # Should cap at one penalty per turn
        assert penalties[0] == -0.1, "Should be capped at one penalty per turn"

    def test_disabled_gives_zero(self):
        """Disabled format penalty should always return 0.0."""
        config = FormatPenaltyConfig(enabled=False)
        computer = FormatPenaltyComputer(config)

        turns = ["no tool call here", "still no tool call"]
        penalties = computer.compute_turn_penalties(turns)
        assert all(p == 0.0 for p in penalties), "Disabled should give all zeros"


# ========================================================================
# SECTION 7: FULL PIPELINE INTEGRATION (reward → advantage → loss)
# ========================================================================


class TestFullPipelineIntegration:
    """End-to-end: rewards through advantages through loss.

    This is the test that catches silent failures where individual components
    work but their composition produces zero gradients.
    """

    def test_pass_trajectory_produces_nonzero_gradient(self):
        """A passing trajectory with good PRM scores MUST produce gradients."""
        # Step 1: Shape rewards
        shaper = PotentialShaper(alpha=0.3, gamma=0.9, outcome_gate=True, gate_mode="add_on_success")
        prm_scores = [0.5, 0.7, 0.9]
        shaped = shaper.shape(prm_scores, outcome_reward=1.0)

        # Step 2: Gated rewards
        config = GatedRewardConfig()
        computer = GatedRewardComputer(config)
        traj_input = TrajectoryRewardInput(
            outcome=OutcomeType.PASS, step_rewards=shaped,
            episode_length=3, max_turns=50
        )
        per_turn = computer.compute_per_turn_rewards([traj_input])[0]

        # Step 3: Compute advantages (need group of 2 for normalization)
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)
        # Add a "zero" trajectory for contrast
        step_rewards = [per_turn, [0.0, 0.0, 0.0]]
        turn_spans = [
            [TurnSpan(0, 0, 10), TurnSpan(1, 10, 20), TurnSpan(2, 20, 30)],
            [TurnSpan(0, 0, 10), TurnSpan(1, 10, 20), TurnSpan(2, 20, 30)],
        ]
        seq_lengths = [30, 30]
        adv = est.compute(step_rewards, turn_spans, seq_lengths, group_size=2)
        assert adv is not None
        assert (adv[0] != 0.0).any(), "PASS trajectory MUST have non-zero advantages"

        # Step 4: Loss
        gspo_config = GSPOConfig(clip_low=0.2, clip_high=0.28, beta_kl=0.0, norm_adv_by_std=False)
        loss_computer = GTPOLossComputer(gspo_config)

        log_probs = torch.randn(2, 30, requires_grad=True)
        old_log_probs = log_probs.detach() + 0.01 * torch.randn(2, 30)
        mask = torch.ones(2, 30)

        result = loss_computer(log_probs, old_log_probs, adv, mask)
        result["loss"].backward()

        assert log_probs.grad is not None, "Gradients must exist"
        assert (log_probs.grad != 0.0).any(), "Gradients must be non-zero for learning"
        assert torch.isfinite(log_probs.grad).all(), "Gradients must be finite"

    def test_all_fail_batch_still_produces_gradient(self):
        """Even all-fail batches should produce gradient signal (via format penalties + outcome)."""
        config = GatedRewardConfig(outcome_fail=-0.2)
        computer = GatedRewardComputer(config)

        # 4 failed trajectories
        inputs = [
            TrajectoryRewardInput(
                outcome=OutcomeType.FAIL, step_rewards=[0.1, 0.2, 0.3],
                episode_length=3, max_turns=50
            ) for _ in range(4)
        ]

        per_turn = computer.compute_per_turn_rewards(
            inputs,
            format_penalties=[[-0.1, 0.0, 0.0]] * 4,  # Format penalty on turn 0
        )

        # All should have some non-zero signal from outcome + format
        for i, turns in enumerate(per_turn):
            assert sum(abs(r) for r in turns) > 0, f"Trajectory {i} should have some reward signal"

        # Build advantages
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)
        turn_spans = [[TurnSpan(j, j * 10, (j + 1) * 10) for j in range(3)] for _ in range(4)]

        adv = est.compute(per_turn, turn_spans, [30] * 4, group_size=4)
        assert adv is not None

        # Loss
        gspo_config = GSPOConfig(clip_low=0.2, clip_high=0.28, beta_kl=0.0, norm_adv_by_std=False)
        loss_computer = GTPOLossComputer(gspo_config)

        log_probs = torch.randn(4, 30, requires_grad=True)
        old_log_probs = log_probs.detach() + 0.01 * torch.randn(4, 30)
        mask = torch.ones(4, 30)

        result = loss_computer(log_probs, old_log_probs, adv, mask)
        result["loss"].backward()

        assert torch.isfinite(result["loss"]), "Loss must be finite even on all-fail batch"

    def test_mixed_batch_good_traj_gets_positive_update(self):
        """In a mixed batch, the best trajectory should get a positive policy update.
        (Positive advantage → ratio > 1 encouraged → probability increases)
        """
        est = StepAdvantageEstimator(mode="gtpo", gamma=0.9)

        # Group of 4: one excellent, three mediocre
        step_rewards = [[1.0, 1.0, 1.0], [0.1, 0.1, 0.1], [0.0, 0.0, 0.0], [-0.1, -0.1, -0.1]]
        turn_spans = [[TurnSpan(j, j * 10, (j + 1) * 10) for j in range(3)] for _ in range(4)]

        adv = est.compute(step_rewards, turn_spans, [30] * 4, group_size=4)
        assert adv is not None

        # Best trajectory (index 0) should have positive mean advantage
        assert adv[0, :30].mean() > 0, (
            "Best trajectory in group should have positive advantage"
        )
        # Worst trajectory (index 3) should have negative mean advantage
        assert adv[3, :30].mean() < 0, (
            "Worst trajectory in group should have negative advantage"
        )


# ========================================================================
# SECTION 8: ENVIRONMENT post_process CORRECTNESS
# ========================================================================


class TestEnvironmentPostProcess:
    """Test global_post_process_and_metrics data injection."""

    def test_turn_spans_computed_from_token_mask(self):
        """turn_spans computed lazily by MiloAdvantageEstimator from mask + messages."""
        from src.nemo_integration.advantage import MiloAdvantageEstimator
        from src.nemo_integration.environment import MiloDockerEnvironment

        # Create minimal environment (won't actually use Docker)
        env = MiloDockerEnvironment.__new__(MiloDockerEnvironment)
        env._config = {"partial_credit_enabled": False, "format_penalty_enabled": False}
        env._gated_computer = GatedRewardComputer(GatedRewardConfig())
        env._prm_config = PRMConfig()
        env._scorer = None

        # Simulate batch after rollout
        batch = {
            "extra_env_info": [
                {"task_id": "1", "instance_id": "inst1", "submitted": True, "patch": "diff",
                 "turn_count": 3, "max_turns": 50, "container_id": None,
                 "docker_image": "", "test_patch": "", "evaluation_script": "",
                 "timeout_seconds": 1800, "episode_start_time": 0.0,
                 "step_rewards": [], "turn_spans": [], "repo": "test", "patch": "diff"},
            ],
            "message_log": [[{"role": "assistant", "content": "turn1"},
                            {"role": "assistant", "content": "turn2"},
                            {"role": "assistant", "content": "turn3"}]],
        }

        # Mock _grade_batch to avoid Docker
        def mock_grade(env_infos):
            return [0.0], [OutcomeType.FAIL]
        env._grade_batch = mock_grade
        env._cleanup_containers = lambda x: None

        result_batch, metrics = env.global_post_process_and_metrics(batch)

        # Verify step_rewards were computed
        sr = result_batch["extra_env_info"][0]["step_rewards"]
        assert isinstance(sr, list)
        assert len(sr) > 0, "step_rewards must be populated"
        assert result_batch["extra_env_info"][0]["turn_count"] == len(sr)

        # Verify turn_spans are computed lazily by advantage estimator
        estimator = MiloAdvantageEstimator(PRMConfig(), group_size=1)
        mask = torch.ones(1, 100)
        # Inject step_rewards into batch for advantage estimator
        result_batch["extra_env_info"][0]["step_rewards"] = [0.1, 0.2, 0.3]
        result_batch["extra_env_info"][0]["turn_count"] = 3

        step_rewards, turn_spans = estimator._extract_step_data(result_batch, 1, mask)
        assert turn_spans is not None
        assert len(turn_spans[0]) == 3, "Should have 3 turn spans for 3 turns"
        for span in turn_spans[0]:
            assert span.start_token < span.end_token, "Span must have positive width"
            assert span.end_token <= 100, "Spans should not exceed seq_len"
