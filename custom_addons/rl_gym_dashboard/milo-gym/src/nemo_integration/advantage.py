"""MiloAdvantageEstimator — NeMo-RL advantage estimator wrapping StepAdvantageEstimator.

Conforms to NeMo-RL's duck-typed AdvantageEstimator interface:
    compute_advantage(prompt_ids, rewards, mask, repeated_batch=None, ...) -> [B, seq_len]

Our StepAdvantageEstimator produces per-token advantages from PRM step rewards,
unlike NeMo-RL's built-in GRPO estimator which broadcasts a scalar per sequence.
"""

from __future__ import annotations

import logging
from typing import Any

import torch
from torch import Tensor

from src.core.config import PRMConfig
from src.prm.step_advantage import StepAdvantageEstimator, TurnSpan

log = logging.getLogger(__name__)


class MiloAdvantageEstimator:
    """Per-token step advantage estimator for NeMo-RL's GRPO loop.

    Extracts step_rewards and turn_spans from repeated_batch["extra_env_info"],
    delegates to StepAdvantageEstimator, and returns [B, seq_len] advantages.

    Falls back to group-relative (RLOO) normalization when step rewards are unavailable.
    """

    def __init__(self, prm_config: PRMConfig, group_size: int) -> None:
        self._estimator = StepAdvantageEstimator.from_config(prm_config)
        self._group_size = group_size

    def compute_advantage(
        self,
        prompt_ids: Tensor,
        rewards: Tensor,
        mask: Tensor,
        repeated_batch: dict[str, Any] | None = None,
        logprobs_policy: Tensor | None = None,
        logprobs_reference: Tensor | None = None,
        **kwargs: Any,
    ) -> Tensor:
        """Compute per-token advantages from PRM step rewards.

        Args:
            prompt_ids: [B, seq_len] prompt token IDs (for grouping by prompt)
            rewards: [B] scalar rewards per sequence
            mask: [B, seq_len] combined token+sample mask
            repeated_batch: Full BatchedDataDict — contains extra_env_info with
                            step_rewards and turn_spans per sample

        Returns:
            [B, seq_len] tensor with per-token advantages
        """
        batch_size, seq_len = mask.shape

        # Try extracting PRM step rewards from environment metadata
        step_rewards, turn_spans = self._extract_step_data(repeated_batch, batch_size, mask)

        if step_rewards is not None and turn_spans is not None:
            seq_lengths = mask.sum(dim=1).long().tolist()
            advantages = self._estimator.compute(
                step_rewards=step_rewards,
                turn_spans=turn_spans,
                seq_lengths=seq_lengths,
                group_size=self._group_size,
            )
            if advantages is not None:
                # Pad/trim to match seq_len
                if advantages.shape[1] < seq_len:
                    pad = torch.zeros(batch_size, seq_len - advantages.shape[1])
                    advantages = torch.cat([advantages, pad], dim=1)
                elif advantages.shape[1] > seq_len:
                    advantages = advantages[:, :seq_len]
                return advantages.to(mask.device)

        # Fallback: GRPO-style group-relative advantage (scalar per sequence)
        return self._rloo_fallback(prompt_ids, rewards, mask)

    def _extract_step_data(
        self,
        repeated_batch: dict[str, Any] | None,
        batch_size: int,
        mask: Tensor | None = None,
    ) -> tuple[list[list[float]] | None, list[list[TurnSpan]] | None]:
        if repeated_batch is None:
            return None, None

        extra_env_info = repeated_batch.get("extra_env_info")
        if extra_env_info is None or len(extra_env_info) == 0:
            return None, None

        message_logs = repeated_batch.get("message_log", [])
        step_rewards: list[list[float]] = []
        turn_spans: list[list[TurnSpan]] = []

        for i in range(min(batch_size, len(extra_env_info))):
            info = extra_env_info[i]
            if info is None:
                step_rewards.append([])
                turn_spans.append([])
                continue

            sr = info.get("step_rewards", [])
            step_rewards.append(sr if isinstance(sr, list) else [])

            # Compute turn_spans from token mask + message structure
            ts_raw = info.get("turn_spans", [])
            if ts_raw:
                spans = []
                for ts in ts_raw:
                    if isinstance(ts, TurnSpan):
                        spans.append(ts)
                    elif isinstance(ts, dict):
                        spans.append(TurnSpan(
                            turn_idx=ts.get("turn_idx", 0),
                            start_token=ts.get("start_token", 0),
                            end_token=ts.get("end_token", 0),
                        ))
                turn_spans.append(spans)
            else:
                n_turns = info.get("turn_count", len(sr))
                spans = self._compute_turn_spans(
                    i, n_turns, mask,
                    message_logs[i] if i < len(message_logs) else [],
                )
                turn_spans.append(spans)

        while len(step_rewards) < batch_size:
            step_rewards.append([])
            turn_spans.append([])

        if not any(sr for sr in step_rewards):
            return None, None

        return step_rewards, turn_spans

    def _compute_turn_spans(
        self,
        sample_idx: int,
        n_turns: int,
        mask: Tensor | None,
        messages: list[dict[str, str]],
    ) -> list[TurnSpan]:
        """Compute token-level turn boundaries from mask + message structure."""
        if n_turns == 0 or mask is None:
            return []

        seq_len = mask.shape[1]
        total_tokens = int(mask[sample_idx].sum().item())

        # Count prompt tokens (non-assistant prefix)
        # Heuristic: assistant turns are the response portion
        assistant_char_lengths = [
            max(1, len(m.get("content", "")))
            for m in (messages if isinstance(messages, list) else [])
            if m.get("role") == "assistant"
        ]

        if len(assistant_char_lengths) >= n_turns and sum(assistant_char_lengths) > 0:
            total_chars = sum(assistant_char_lengths[:n_turns])
            spans = []
            cursor = 0
            for j in range(n_turns):
                proportion = assistant_char_lengths[j] / total_chars
                turn_tokens = max(1, int(proportion * total_tokens))
                end = min(cursor + turn_tokens, total_tokens) if j < n_turns - 1 else total_tokens
                end = min(end, seq_len)
                spans.append(TurnSpan(turn_idx=j, start_token=cursor, end_token=end))
                cursor = end
            return spans

        # Even division fallback
        tokens_per_turn = max(1, total_tokens // n_turns)
        spans = []
        for j in range(n_turns):
            start = j * tokens_per_turn
            end = (j + 1) * tokens_per_turn if j < n_turns - 1 else total_tokens
            end = min(end, seq_len)
            spans.append(TurnSpan(turn_idx=j, start_token=start, end_token=end))
        return spans

    def _rloo_fallback(
        self,
        prompt_ids: Tensor,
        rewards: Tensor,
        mask: Tensor,
    ) -> Tensor:
        """Leave-one-out group-relative advantage (GRPO default)."""
        batch_size, seq_len = mask.shape
        rewards = rewards.to(mask.device)
        advantages = torch.zeros(batch_size, seq_len, device=mask.device)

        num_groups = max(1, batch_size // self._group_size)

        for g in range(num_groups):
            start = g * self._group_size
            end = min(start + self._group_size, batch_size)
            group_rewards = rewards[start:end]

            if len(group_rewards) < 2:
                continue

            std = group_rewards.std()
            if std < 1e-8:
                # All same reward → no learning signal from this group
                # Skip normalization (advantages stay at 0)
                log.debug("Group %d has zero reward variance — no learning signal", g)
                continue

            for i in range(start, end):
                others_mask = torch.ones(end - start, dtype=torch.bool, device=rewards.device)
                others_mask[i - start] = False
                baseline = group_rewards[others_mask].mean()
                adv = (rewards[i] - baseline) / (std + 1e-8)
                advantages[i] = adv

        return advantages
