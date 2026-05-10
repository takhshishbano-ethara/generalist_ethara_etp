from __future__ import annotations

import logging
from dataclasses import dataclass

import torch

from src.core.config import PRMConfig

log = logging.getLogger(__name__)


@dataclass
class TurnSpan:
    turn_idx: int
    start_token: int
    end_token: int


@dataclass
class StepAdvantageEstimator:
    mode: str = "hybrid"
    min_group_variance: float = 0.01
    gamma: float = 0.9

    @classmethod
    def from_config(cls, config: PRMConfig) -> StepAdvantageEstimator:
        return cls(
            mode=config.advantage_mode,
            min_group_variance=config.min_group_variance,
            gamma=config.gtpo_gamma,
        )

    def should_override_verl(self) -> bool:
        return self.mode in ("step_wise", "hybrid", "gtpo")

    def compute(
        self,
        step_rewards: list[list[float]],
        turn_spans: list[list[TurnSpan]],
        seq_lengths: list[int],
        group_size: int,
    ) -> torch.Tensor | None:
        if self.mode == "rloo":
            return None

        batch_size = len(step_rewards)
        if batch_size == 0:
            return torch.zeros(0, 0)

        max_seq_len = max(seq_lengths) if seq_lengths else 0

        if self.mode == "hybrid":
            return self._hybrid(step_rewards, seq_lengths, group_size, max_seq_len)
        elif self.mode == "step_wise":
            return self._step_wise(step_rewards, turn_spans, seq_lengths, group_size, max_seq_len)
        elif self.mode == "gtpo":
            return self._gtpo(step_rewards, turn_spans, seq_lengths, group_size, max_seq_len)
        return None

    def compute_from_batch(
        self,
        extra_env_info: list[dict],
        mask: torch.Tensor,
        group_size: int,
    ) -> torch.Tensor | None:
        step_rewards = []
        turn_spans = []
        for info in extra_env_info:
            sr = info.get("step_rewards", [])
            step_rewards.append(sr)
            raw_spans = info.get("turn_spans", [])
            spans = [
                TurnSpan(turn_idx=s["turn_idx"], start_token=s["start_token"], end_token=s["end_token"])
                if isinstance(s, dict) else s
                for s in raw_spans
            ]
            turn_spans.append(spans)

        seq_lengths = mask.sum(dim=1).int().tolist()
        return self.compute(step_rewards, turn_spans, seq_lengths, group_size)

    def _hybrid(
        self,
        step_rewards: list[list[float]],
        seq_lengths: list[int],
        group_size: int,
        max_seq_len: int,
    ) -> torch.Tensor:
        batch_size = len(step_rewards)
        advantages = torch.zeros(batch_size, max_seq_len)
        returns = torch.tensor([sum(sr) for sr in step_rewards], dtype=torch.float32)

        n_groups = -(-batch_size // group_size) if group_size > 0 else 1

        for g in range(n_groups):
            start = g * group_size
            end = min(start + group_size, batch_size)
            group_returns = returns[start:end]

            if len(group_returns) < 2:
                continue

            group_std = group_returns.std().item()
            if group_std < 1e-8:
                continue

            for i in range(start, end):
                local_idx = i - start
                mask = torch.ones(end - start, dtype=torch.bool)
                mask[local_idx] = False
                baseline = group_returns[mask].mean()
                adv = (returns[i] - baseline) / max(group_std, self.min_group_variance)
                advantages[i, :seq_lengths[i]] = adv

        return advantages

    def _step_wise(
        self,
        step_rewards: list[list[float]],
        turn_spans: list[list[TurnSpan]],
        seq_lengths: list[int],
        group_size: int,
        max_seq_len: int,
    ) -> torch.Tensor:
        batch_size = len(step_rewards)
        advantages = torch.zeros(batch_size, max_seq_len)

        n_groups = -(-batch_size // group_size) if group_size > 0 else 1
        max_steps = max((len(sr) for sr in step_rewards), default=0)

        for g in range(n_groups):
            start = g * group_size
            end = min(start + group_size, batch_size)

            for step_idx in range(max_steps):
                values: list[float] = []
                indices: list[int] = []
                for i in range(start, end):
                    if step_idx < len(step_rewards[i]):
                        values.append(step_rewards[i][step_idx])
                        indices.append(i)

                if len(values) < 2:
                    for i in indices:
                        if step_idx < len(turn_spans[i]):
                            span = turn_spans[i][step_idx]
                            advantages[i, span.start_token : span.end_token] = step_rewards[i][step_idx]
                    continue

                mean_val = sum(values) / len(values)
                variance = sum((v - mean_val) ** 2 for v in values) / len(values)
                std = variance**0.5

                if std < self.min_group_variance:
                    continue

                for i in indices:
                    if step_idx < len(turn_spans[i]):
                        span = turn_spans[i][step_idx]
                        raw = step_rewards[i][step_idx]
                        normalized = (raw - mean_val) / (std + 1e-8)
                        advantages[i, span.start_token : span.end_token] = normalized

        return advantages

    def _gtpo(
        self,
        step_rewards: list[list[float]],
        turn_spans: list[list[TurnSpan]],
        seq_lengths: list[int],
        group_size: int,
        max_seq_len: int,
    ) -> torch.Tensor:
        """GTPO discounted returns with group-relative normalization.

        R_{i,j} = Σ_{m=j}^{T} γ^{m-j} · r_{i,m}
        Advantages normalized per-group (not globally).
        Uses O(T) reverse accumulation instead of O(T²) nested loop.
        """
        batch_size = len(step_rewards)
        advantages = torch.zeros(batch_size, max_seq_len)

        all_returns: list[list[float]] = []
        for i in range(batch_size):
            T = len(step_rewards[i])
            returns_i: list[float] = [0.0] * T
            if T > 0:
                returns_i[T - 1] = step_rewards[i][T - 1]
                for j in range(T - 2, -1, -1):
                    returns_i[j] = step_rewards[i][j] + self.gamma * returns_i[j + 1]
            all_returns.append(returns_i)

        n_groups = -(-batch_size // group_size) if group_size > 0 else 1

        for g in range(n_groups):
            start = g * group_size
            end = min(start + group_size, batch_size)

            group_flat: list[float] = []
            for i in range(start, end):
                group_flat.extend(all_returns[i])

            if len(group_flat) < 2:
                for i in range(start, end):
                    for j, R_j in enumerate(all_returns[i]):
                        if j < len(turn_spans[i]):
                            span = turn_spans[i][j]
                            advantages[i, span.start_token:span.end_token] = R_j
                continue

            mean_R = sum(group_flat) / len(group_flat)
            var_R = sum((r - mean_R) ** 2 for r in group_flat) / len(group_flat)
            std_R = max(var_R ** 0.5, 1e-8)

            for i in range(start, end):
                for j, R_j in enumerate(all_returns[i]):
                    A_j = (R_j - mean_R) / std_R
                    if j < len(turn_spans[i]):
                        span = turn_spans[i][j]
                        advantages[i, span.start_token:span.end_token] = A_j

        return advantages
