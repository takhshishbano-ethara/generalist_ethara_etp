"""PivotRL turn selection (arXiv 2603.21383) — identifies high-variance pivot turns for training."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import torch

from src.core.config import GatedRewardConfig
from src.core.schemas import Trajectory

log = logging.getLogger(__name__)


@dataclass
class TurnStatistics:
    """Per-turn reward statistics across a group: shape [max_turns] each."""

    variance: torch.Tensor
    mean: torch.Tensor
    valid_mask: torch.Tensor
    num_valid: torch.Tensor


class PivotSelector:
    """Selects pivot turns where σ²(reward) > threshold AND μ(reward) < difficulty.

    Fallback: if no pivots found for a group, all valid turns are included.
    """

    def __init__(self, config: GatedRewardConfig) -> None:
        self.variance_threshold = config.pivot_variance_threshold
        self.difficulty_threshold = config.pivot_difficulty_threshold

    def select_pivot_turns(
        self,
        trajectory_groups: list[list[Trajectory]],
    ) -> torch.Tensor:
        """Return boolean mask [num_groups, max_turns] of turns to train on."""
        if not trajectory_groups:
            return torch.zeros(0, 0, dtype=torch.bool)

        max_turns = self._compute_max_turns(trajectory_groups)
        num_groups = len(trajectory_groups)

        mask = torch.zeros(num_groups, max_turns, dtype=torch.bool)
        total_turns = 0
        selected_turns = 0

        for group_idx, group in enumerate(trajectory_groups):
            stats = self.compute_turn_statistics(group, max_turns)

            high_variance = stats.variance > self.variance_threshold
            below_difficulty = stats.mean < self.difficulty_threshold
            pivot_mask = high_variance & below_difficulty & stats.valid_mask

            valid_count = int(stats.valid_mask.sum().item())
            pivot_count = int(pivot_mask.sum().item())
            total_turns += valid_count

            if pivot_count == 0:
                mask[group_idx] = stats.valid_mask
                selected_turns += valid_count
            else:
                mask[group_idx] = pivot_mask
                selected_turns += pivot_count

        pivot_fraction = selected_turns / max(total_turns, 1)
        log.debug(
            "PivotRL selection: %d/%d turns (%.1f%%) across %d groups",
            selected_turns,
            total_turns,
            pivot_fraction * 100,
            num_groups,
        )

        return mask

    def compute_turn_statistics(
        self,
        group: list[Trajectory],
        max_turns: int,
    ) -> TurnStatistics:
        """Compute per-turn reward variance and mean across rollouts in a group."""
        group_size = len(group)
        if group_size == 0:
            zeros = torch.zeros(max_turns)
            return TurnStatistics(
                variance=zeros,
                mean=zeros,
                valid_mask=torch.zeros(max_turns, dtype=torch.bool),
                num_valid=zeros,
            )

        reward_matrix = torch.full(
            (group_size, max_turns), float("nan"), dtype=torch.float32
        )

        for rollout_idx, trajectory in enumerate(group):
            rewards = self._extract_turn_rewards(trajectory)
            num_turns = min(len(rewards), max_turns)
            if num_turns > 0:
                reward_matrix[rollout_idx, :num_turns] = torch.tensor(
                    rewards[:num_turns], dtype=torch.float32
                )

        valid_counts = (~torch.isnan(reward_matrix)).sum(dim=0).float()
        valid_mask = valid_counts >= 2

        rewards_filled = reward_matrix.clone()
        rewards_filled[torch.isnan(rewards_filled)] = 0.0

        # μ = Σx / n
        reward_sum = rewards_filled.sum(dim=0)
        mean = torch.where(
            valid_counts > 0,
            reward_sum / valid_counts,
            torch.zeros(max_turns),
        )

        # σ² = E[X²] - (E[X])²
        rewards_sq_sum = (rewards_filled**2).sum(dim=0)
        mean_sq = torch.where(
            valid_counts > 0,
            rewards_sq_sum / valid_counts,
            torch.zeros(max_turns),
        )
        variance = torch.where(
            valid_mask,
            (mean_sq - mean**2).clamp(min=0.0),
            torch.zeros(max_turns),
        )

        return TurnStatistics(
            variance=variance,
            mean=mean,
            valid_mask=valid_mask,
            num_valid=valid_counts,
        )

    def _extract_turn_rewards(self, trajectory: Trajectory) -> list[float]:
        """Use step_rewards if available, otherwise broadcast final reward."""
        if trajectory.step_rewards:
            return trajectory.step_rewards
        num_turns = len(trajectory.turns)
        if num_turns == 0:
            return []
        return [trajectory.reward] * num_turns

    def _compute_max_turns(
        self, trajectory_groups: list[list[Trajectory]]
    ) -> int:
        max_t = 0
        for group in trajectory_groups:
            for trajectory in group:
                turn_count = len(trajectory.step_rewards) if trajectory.step_rewards else len(trajectory.turns)
                max_t = max(max_t, turn_count)
        return max_t
