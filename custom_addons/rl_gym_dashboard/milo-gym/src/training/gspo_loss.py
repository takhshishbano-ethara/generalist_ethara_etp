"""GSPO (Group Sequence Policy Optimization) loss with segment-level importance ratios.

Implements the clipped surrogate loss from arXiv:2507.18071, extended to multi-turn
episodes where each assistant turn is treated as a separate segment for ratio computation.

Mathematical formulation:
    Sequence-level ratio:
        s_i(theta) = exp(1/|y_i| * sum_t log[pi_theta(y_{i,t}|x,y_{i,<t}) / pi_old(y_{i,t}|x,y_{i,<t})])

    For multi-turn (segment-level):
        For each segment k in episode i:
            r_k = exp(mean_{t in segment_k}(log_pi_theta - log_pi_old))

    Clipped surrogate:
        L_i = min(s_i * A_i, clip(s_i, 1 - eps_low, 1 + eps_high) * A_i)

    Dual clip (when enabled):
        If A_i < 0: L_i = max(L_i, c * A_i)  where c > 1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn.functional as F
from torch import Tensor

from src.core.config import GSPOConfig

log = logging.getLogger(__name__)


def compute_segment_masked_mean(
    log_ratios: Tensor,
    response_mask: Tensor,
    segment_ids: Tensor,
) -> tuple[Tensor, Tensor]:
    """Compute per-segment mean log-ratios for multi-turn episodes.

    For each contiguous segment of response tokens (identified by segment_ids),
    computes mean(log_ratio) over that segment. Segments with id=0 are treated
    as prompt/padding and excluded.

    Args:
        log_ratios: Token-level log-probability ratios [batch, seq_len].
        response_mask: Binary mask indicating response tokens [batch, seq_len].
        segment_ids: Integer tensor identifying segment membership [batch, seq_len].
            0 = prompt/padding, 1 = turn1, 2 = turn2, etc.

    Returns:
        segment_mean_log_ratios: Per-segment mean log-ratio [batch, max_segments].
            Padded segments have value 0.0.
        segment_mask: Binary mask for valid segments [batch, max_segments].

    Mathematical notation:
        For segment k: mean_log_ratio_k = (1/|S_k|) * sum_{t in S_k} log(pi_theta/pi_old)
    """
    batch_size, seq_len = log_ratios.shape
    device = log_ratios.device

    masked_log_ratios = log_ratios * response_mask

    max_seg_id = segment_ids.max()

    if max_seg_id == 0:
        return (
            torch.zeros(batch_size, 1, device=device),
            torch.zeros(batch_size, 1, device=device),
        )

    num_segments = int(max_seg_id.item())

    segment_mean_log_ratios = torch.zeros(batch_size, num_segments, device=device)
    segment_mask = torch.zeros(batch_size, num_segments, device=device)

    for seg_idx in range(1, num_segments + 1):
        seg_token_mask = (segment_ids == seg_idx) & (response_mask > 0)
        seg_token_count = seg_token_mask.sum(dim=1).float()
        seg_sum = (masked_log_ratios * seg_token_mask.float()).sum(dim=1)
        valid = seg_token_count > 0
        segment_mean_log_ratios[:, seg_idx - 1] = torch.where(
            valid,
            seg_sum / seg_token_count.clamp(min=1.0),
            torch.zeros_like(seg_sum),
        )
        segment_mask[:, seg_idx - 1] = valid.float()

    return segment_mean_log_ratios, segment_mask


def compute_gspo_loss(
    log_probs: Tensor,
    old_log_probs: Tensor,
    advantages: Tensor,
    response_mask: Tensor,
    segment_ids: Tensor,
    clip_low: float = 3e-4,
    clip_high: float = 4e-4,
    dual_clip: bool = True,
    dual_clip_coef: float = 5.0,
    kl_coef: float = 0.0,
    loss_agg_mode: str = "seq-mean-token-mean",
    norm_adv_by_std: bool = True,
) -> dict[str, Tensor]:
    """Compute GSPO clipped surrogate loss with segment-level importance ratios.

    Implements the core GSPO objective from arXiv:2507.18071 with per-segment
    (per-turn) ratio computation for multi-turn RL episodes.

    Args:
        log_probs: Log-probabilities under current policy [batch, seq_len].
        old_log_probs: Log-probabilities under old (rollout) policy [batch, seq_len].
        advantages: Per-segment advantages [batch, max_segments] or per-token [batch, seq_len].
            If per-token, they are aggregated to segment level internally.
        response_mask: Binary mask for response tokens [batch, seq_len].
        segment_ids: Segment membership per token [batch, seq_len].
            0 = prompt/padding, 1..K = turn indices.
        clip_low: Lower clipping epsilon (default 3e-4 per GSPO paper).
        clip_high: Upper clipping epsilon (default 4e-4 per GSPO paper).
        dual_clip: Whether to apply dual clipping for negative advantages.
        dual_clip_coef: Coefficient c for dual clip: max(L_i, c * A_i) when A_i < 0.
        kl_coef: Coefficient for KL penalty term. 0.0 disables KL penalty.
        loss_agg_mode: Aggregation mode. "seq-mean-token-mean" averages per-segment
            losses then averages across segments.
        norm_adv_by_std: Whether to normalize advantages by their standard deviation.

    Returns:
        Dictionary with:
            - "loss": Scalar policy gradient loss (negated for gradient ascent).
            - "clip_frac": Fraction of segments that were clipped.
            - "approx_kl": Approximate KL divergence for monitoring.
            - "mean_ratio": Mean importance sampling ratio.
            - "kl_penalty": KL penalty term (0 if kl_coef=0).

    Mathematical formulation:
        log_ratio_t = log pi_theta(y_t|...) - log pi_old(y_t|...)
        For segment k: r_k = exp(mean_{t in S_k}(log_ratio_t))
        L_clip = min(r_k * A_k, clip(r_k, 1-eps_low, 1+eps_high) * A_k)
        If dual_clip and A_k < 0: L_clip = max(L_clip, c * A_k)
        Loss = -mean(L_clip)
    """
    batch_size, seq_len = log_probs.shape
    device = log_probs.device

    # --- Step 1: Compute token-level log-ratios ---
    log_ratios = log_probs - old_log_probs  # [batch, seq_len]
    log_ratios = torch.clamp(log_ratios, min=-20.0, max=20.0)

    # --- Step 2: Compute per-segment mean log-ratios ---
    segment_mean_log_ratios, segment_mask = compute_segment_masked_mean(
        log_ratios, response_mask, segment_ids
    )
    # segment_mean_log_ratios: [batch, num_segments]
    # segment_mask: [batch, num_segments]

    num_segments = segment_mean_log_ratios.shape[1]

    # --- Step 3: Compute segment-level importance ratios ---
    # r_k = exp(mean log-ratio for segment k)
    ratios = torch.exp(segment_mean_log_ratios)  # [batch, num_segments]

    # --- Step 4: Align advantages to segment level ---
    if advantages.shape[1] == seq_len:
        # Per-token advantages — aggregate to segment level by taking
        # the mean advantage within each segment
        seg_advantages = torch.zeros(batch_size, num_segments, device=device)
        for seg_idx in range(1, num_segments + 1):
            seg_token_mask = (segment_ids == seg_idx) & (response_mask > 0)
            seg_count = seg_token_mask.sum(dim=1).float().clamp(min=1.0)
            seg_adv_sum = (advantages * seg_token_mask.float()).sum(dim=1)
            seg_advantages[:, seg_idx - 1] = seg_adv_sum / seg_count
    elif advantages.shape[1] >= num_segments:
        seg_advantages = advantages[:, :num_segments]
    else:
        # Pad advantages if fewer than num_segments
        pad_size = num_segments - advantages.shape[1]
        seg_advantages = F.pad(advantages, (0, pad_size), value=0.0)

    # --- Step 5: Normalize advantages ---
    if norm_adv_by_std:
        valid_advs = seg_advantages[segment_mask > 0]
        if valid_advs.numel() > 1:
            adv_std = valid_advs.std().clamp(min=1e-8)
            adv_mean = valid_advs.mean()
            seg_advantages = (seg_advantages - adv_mean) / adv_std

    # --- Step 6: Clipped surrogate objective ---
    clipped_ratios = torch.clamp(ratios, 1.0 - clip_low, 1.0 + clip_high)

    surr1 = ratios * seg_advantages
    surr2 = clipped_ratios * seg_advantages

    # Standard PPO-style min
    policy_loss = torch.min(surr1, surr2)

    # --- Step 7: Dual clip for negative advantages ---
    if dual_clip:
        # When advantage is negative, apply lower bound: max(L, c * A)
        neg_adv_mask = (seg_advantages < 0).float()
        dual_clip_bound = dual_clip_coef * seg_advantages
        policy_loss = torch.where(
            neg_adv_mask.bool(),
            torch.max(policy_loss, dual_clip_bound),
            policy_loss,
        )

    # --- Step 8: Aggregate loss ---
    # Apply segment mask to exclude padded segments
    masked_policy_loss = policy_loss * segment_mask

    if loss_agg_mode == "seq-mean-token-mean":
        # Average loss per sequence (across valid segments), then average sequences
        seq_segment_counts = segment_mask.sum(dim=1).clamp(min=1.0)
        per_seq_loss = masked_policy_loss.sum(dim=1) / seq_segment_counts
        aggregated_loss = per_seq_loss.mean()
    else:
        # Flat mean over all valid segments
        total_valid = segment_mask.sum().clamp(min=1.0)
        aggregated_loss = masked_policy_loss.sum() / total_valid

    # --- Step 9: KL penalty (optional) ---
    kl_penalty = torch.zeros(1, device=device)
    if kl_coef > 0.0:
        # Approximate KL: mean of (ratio - 1 - log(ratio)) per segment
        approx_kl_per_seg = ratios - 1.0 - segment_mean_log_ratios
        kl_penalty = (approx_kl_per_seg * segment_mask).sum() / segment_mask.sum().clamp(min=1.0)
        aggregated_loss = aggregated_loss - kl_coef * kl_penalty

    # Negate for gradient ascent (maximize objective = minimize negative)
    loss = -aggregated_loss

    # --- Monitoring metrics ---
    with torch.no_grad():
        clip_frac = (
            ((ratios < (1.0 - clip_low)) | (ratios > (1.0 + clip_high))).float()
            * segment_mask
        ).sum() / segment_mask.sum().clamp(min=1.0)

        approx_kl = (
            (ratios - 1.0 - segment_mean_log_ratios) * segment_mask
        ).sum() / segment_mask.sum().clamp(min=1.0)

        mean_ratio = (ratios * segment_mask).sum() / segment_mask.sum().clamp(min=1.0)

    return {
        "loss": loss,
        "clip_frac": clip_frac,
        "approx_kl": approx_kl,
        "mean_ratio": mean_ratio,
        "kl_penalty": kl_penalty.squeeze(),
    }


class GSPOLossComputer:
    """Wraps GSPO loss computation with config-driven parameters.

    Provides a clean API for the trainer to compute the GSPO clipped surrogate
    loss with segment-level importance ratios for multi-turn episodes.

    Usage:
        config = GSPOConfig(clip_low=3e-4, clip_high=4e-4)
        loss_computer = GSPOLossComputer(config)
        result = loss_computer(log_probs, old_log_probs, advantages, response_mask, segment_ids)
        loss = result["loss"]
    """

    def __init__(
        self,
        config: GSPOConfig,
        dual_clip: bool = True,
        dual_clip_coef: float = 5.0,
        kl_coef: Optional[float] = None,
    ) -> None:
        """Initialize GSPO loss computer from config.

        Args:
            config: GSPOConfig dataclass with clip_low, clip_high, etc.
            dual_clip: Enable dual clipping for negative advantages.
            dual_clip_coef: Coefficient for dual clip lower bound.
            kl_coef: Override KL penalty coefficient. If None, uses config.beta_kl.
        """
        self.clip_low: float = config.clip_low
        self.clip_high: float = config.clip_high
        self.norm_adv_by_std: bool = config.norm_adv_by_std
        self.loss_agg_mode: str = config.loss_agg_mode
        self.importance_sampling: str = config.importance_sampling
        self.dual_clip: bool = dual_clip
        self.dual_clip_coef: float = dual_clip_coef
        self.kl_coef: float = kl_coef if kl_coef is not None else config.beta_kl

    def __call__(
        self,
        log_probs: Tensor,
        old_log_probs: Tensor,
        advantages: Tensor,
        response_mask: Tensor,
        segment_ids: Tensor,
    ) -> dict[str, Tensor]:
        """Compute GSPO loss.

        Args:
            log_probs: Current policy log-probs [batch, seq_len].
            old_log_probs: Reference policy log-probs [batch, seq_len].
            advantages: Advantages [batch, max_segments] or [batch, seq_len].
            response_mask: Binary response mask [batch, seq_len].
            segment_ids: Segment IDs per token [batch, seq_len].

        Returns:
            Dict with "loss", "clip_frac", "approx_kl", "mean_ratio", "kl_penalty".
        """
        return compute_gspo_loss(
            log_probs=log_probs,
            old_log_probs=old_log_probs,
            advantages=advantages,
            response_mask=response_mask,
            segment_ids=segment_ids,
            clip_low=self.clip_low,
            clip_high=self.clip_high,
            dual_clip=self.dual_clip,
            dual_clip_coef=self.dual_clip_coef,
            kl_coef=self.kl_coef,
            loss_agg_mode=self.loss_agg_mode,
            norm_adv_by_std=self.norm_adv_by_std,
        )

    def compute_token_level_loss(
        self,
        log_probs: Tensor,
        old_log_probs: Tensor,
        advantages: Tensor,
        response_mask: Tensor,
    ) -> dict[str, Tensor]:
        """Fallback: standard token-level PPO loss without segment grouping.

        Used when importance_sampling="token" in config. Each token is its own
        "segment" with ratio = exp(log_prob - old_log_prob).

        Args:
            log_probs: Current policy log-probs [batch, seq_len].
            old_log_probs: Reference policy log-probs [batch, seq_len].
            advantages: Per-token advantages [batch, seq_len].
            response_mask: Binary response mask [batch, seq_len].

        Returns:
            Dict with "loss", "clip_frac", "approx_kl", "mean_ratio", "kl_penalty".
        """
        log_ratios = log_probs - old_log_probs
        log_ratios = torch.clamp(log_ratios, min=-20.0, max=20.0)
        ratios = torch.exp(log_ratios)

        # Normalize advantages
        if self.norm_adv_by_std:
            valid_advs = advantages[response_mask > 0]
            if valid_advs.numel() > 1:
                adv_std = valid_advs.std().clamp(min=1e-8)
                adv_mean = valid_advs.mean()
                advantages = (advantages - adv_mean) / adv_std

        clipped_ratios = torch.clamp(ratios, 1.0 - self.clip_low, 1.0 + self.clip_high)

        surr1 = ratios * advantages
        surr2 = clipped_ratios * advantages
        policy_loss = torch.min(surr1, surr2)

        if self.dual_clip:
            neg_mask = advantages < 0
            dual_bound = self.dual_clip_coef * advantages
            policy_loss = torch.where(neg_mask, torch.max(policy_loss, dual_bound), policy_loss)

        masked_loss = policy_loss * response_mask
        total_tokens = response_mask.sum().clamp(min=1.0)
        loss = -(masked_loss.sum() / total_tokens)

        # KL penalty
        kl_penalty = torch.zeros(1, device=log_probs.device)
        if self.kl_coef > 0.0:
            token_kl = (ratios - 1.0 - log_ratios) * response_mask
            kl_penalty = token_kl.sum() / total_tokens
            loss = loss + self.kl_coef * kl_penalty

        with torch.no_grad():
            clip_frac = (
                ((ratios < (1.0 - self.clip_low)) | (ratios > (1.0 + self.clip_high))).float()
                * response_mask
            ).sum() / total_tokens
            approx_kl = ((ratios - 1.0 - log_ratios) * response_mask).sum() / total_tokens
            mean_ratio = (ratios * response_mask).sum() / total_tokens

        return {
            "loss": loss,
            "clip_frac": clip_frac,
            "approx_kl": approx_kl,
            "mean_ratio": mean_ratio,
            "kl_penalty": kl_penalty.squeeze(),
        }
