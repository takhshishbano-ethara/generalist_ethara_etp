"""G-RA (Gated Reward Accumulation) from arXiv:2508.10548.

Gates intermediate step rewards based on terminal outcome:
- If outcome <= gate_threshold, ALL step rewards (format + PRM) are zeroed.
- Only when the model passes tests do step rewards contribute.

Priority order (highest to lowest):
  1. Outcome reward (binary: pass/fail from Docker execution)
  2. Format/scaffold rewards (rule-based)
  3. PRM step scores (model-based per-turn quality)

Length penalty from LHT-SWE (arXiv:2508.03501) penalizes long episodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import torch

from src.core.config import GatedRewardConfig
from src.core.schemas import Trajectory

log = logging.getLogger(__name__)


class OutcomeType(Enum):
    """Terminal outcome classification for a trajectory."""

    PASS = "pass"
    FAIL = "fail"
    EMPTY = "empty"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class TrajectoryRewardInput:
    """Pre-computed reward signals for a single trajectory.

    This is the interface between reward_manager (which computes raw signals)
    and the gated reward computer (which combines them).
    """

    outcome: OutcomeType
    step_rewards: list[float]
    episode_length: int
    max_turns: int


class GatedRewardComputer:
    """Computes final shaped rewards using hierarchical gating (G-RA).

    Gate rule:
        R^(i)(s,a) = 0  if R^(j)(s,a) <= gate_threshold  (for j > i in priority)

    When outcome_reward <= gate_threshold, all step rewards are zeroed.
    Total shaped reward per trajectory:
        if outcome > gate_threshold:
            total = outcome_weight * outcome + step_weight * sum(step_rewards) + penalty
        else:
            total = outcome_weight * outcome + penalty
    """

    def __init__(self, config: GatedRewardConfig) -> None:
        self._config = config
        self._outcome_map: dict[OutcomeType, float] = {
            OutcomeType.PASS: config.outcome_pass,
            OutcomeType.FAIL: config.outcome_fail,
            OutcomeType.EMPTY: config.outcome_empty,
            OutcomeType.TIMEOUT: config.outcome_timeout,
        }

    @property
    def config(self) -> GatedRewardConfig:
        return self._config

    def classify_outcome(self, trajectory: Trajectory) -> OutcomeType:
        """Determine outcome type from trajectory metadata."""
        if trajectory.timed_out or trajectory.hit_max_turns:
            return OutcomeType.TIMEOUT
        if not trajectory.patch:
            return OutcomeType.EMPTY
        if trajectory.reward > 0.0:
            return OutcomeType.PASS
        return OutcomeType.FAIL

    def compute_length_penalty(
        self,
        episode_length: int,
        max_turns: int,
        length_threshold: int | None = None,
    ) -> float:
        """Length penalty from LHT-SWE (arXiv:2508.03501).

        penalty = (length_threshold - episode_length) / (max_turns - length_threshold)
                  when episode_length >= length_threshold, else 0.0

        This is negative when episode_length > length_threshold, penalizing long runs.
        """
        if max_turns <= 0:
            return 0.0

        if length_threshold is None:
            # Default: penalize episodes exceeding 70% of max_turns
            length_threshold = int(max_turns * 0.7)

        if length_threshold >= max_turns:
            # Avoid division by zero; no penalty possible
            return 0.0

        if episode_length < length_threshold:
            return 0.0

        denominator = max_turns - length_threshold
        penalty = (length_threshold - episode_length) / denominator
        return float(penalty)

    def compute_gated_rewards(
        self,
        inputs: list[TrajectoryRewardInput],
    ) -> torch.Tensor:
        """Apply G-RA gate logic to a batch of trajectories.

        Args:
            inputs: Pre-computed reward signals per trajectory.

        Returns:
            Shaped rewards tensor of shape [batch_size].
        """
        if not inputs:
            return torch.zeros(0)

        rewards: list[float] = []

        for traj_input in inputs:
            outcome_reward = self._outcome_map[traj_input.outcome]
            step_sum = self._aggregate_step_rewards(traj_input.step_rewards)
            length_pen = self.compute_length_penalty(
                episode_length=traj_input.episode_length,
                max_turns=traj_input.max_turns,
            )

            # G-RA gate: if outcome <= threshold, zero all step rewards
            if outcome_reward > self._config.gate_threshold:
                total = (
                    outcome_reward
                    + self._config.prm_weight * step_sum
                    + self._config.length_penalty_weight * length_pen
                )
            else:
                # Step rewards gated OFF
                total = outcome_reward + self._config.length_penalty_weight * length_pen

            rewards.append(total)

        return torch.tensor(rewards, dtype=torch.float32)

    def compute_from_trajectories(
        self,
        trajectories: list[Trajectory],
        max_turns: int = 50,
    ) -> torch.Tensor:
        """Convenience: compute gated rewards directly from Trajectory objects.

        Args:
            trajectories: List of completed trajectories with pre-computed step_rewards.
            max_turns: Maximum allowed turns for the current curriculum phase.

        Returns:
            Shaped rewards tensor of shape [batch_size].
        """
        inputs = [
            TrajectoryRewardInput(
                outcome=self.classify_outcome(traj),
                step_rewards=traj.step_rewards,
                episode_length=traj.episode_length,
                max_turns=max_turns,
            )
            for traj in trajectories
        ]
        return self.compute_gated_rewards(inputs)

    def _aggregate_step_rewards(self, step_rewards: list[float]) -> float:
        """Sum step rewards, handling empty case."""
        if not step_rewards:
            return 0.0
        return sum(step_rewards)

    def compute_per_turn_rewards(
        self,
        inputs: list[TrajectoryRewardInput],
        partial_credits: list[float] | None = None,
        format_penalties: list[list[float]] | None = None,
    ) -> list[list[float]]:
        """Return per-turn rewards for GTPO discounted returns.

        When gate is open (PASS): step_rewards preserved as-is (already shaped by PotentialShaper).
        When gate is closed (FAIL/EMPTY/TIMEOUT): partial_credit on last turn + format penalties.
        Length penalty always applied to last turn.
        Format penalties applied to each turn regardless of gate.
        """
        result: list[list[float]] = []
        for idx, traj_input in enumerate(inputs):
            outcome_reward = self._outcome_map[traj_input.outcome]
            gate_open = outcome_reward > self._config.gate_threshold

            if gate_open and traj_input.step_rewards:
                turn_rewards = list(traj_input.step_rewards)
                turn_rewards[-1] += outcome_reward
            else:
                n_turns = len(traj_input.step_rewards) if traj_input.step_rewards else 1
                turn_rewards = [0.0] * n_turns
                if turn_rewards:
                    turn_rewards[-1] += outcome_reward
                    # Add partial credit for failures (GTPO paper: self-supervised shaping)
                    if partial_credits and idx < len(partial_credits):
                        turn_rewards[-1] += partial_credits[idx]

            # Apply format penalties per-turn (paper: r_format = -0.1 per violation)
            if format_penalties and idx < len(format_penalties):
                turn_fmt = format_penalties[idx]
                for t in range(min(len(turn_rewards), len(turn_fmt))):
                    turn_rewards[t] += turn_fmt[t]

            length_pen = self.compute_length_penalty(
                episode_length=traj_input.episode_length,
                max_turns=traj_input.max_turns,
            )
            if turn_rewards:
                turn_rewards[-1] += self._config.length_penalty_weight * length_pen

            result.append(turn_rewards)
        return result
