"""GTPO (Group Turn-level Policy Optimization) loss.

Token-level importance ratios with per-turn advantages from discounted returns.
Unlike GSPO's segment-level geometric-mean ratios, GTPO uses standard per-token
ratios w_t = exp(log_prob_t - old_log_prob_t) with asymmetric clipping and dual clip.

Reference: arXiv:2511.14846
"""

from __future__ import annotations

import logging

import torch
from torch import Tensor

from src.core.config import GSPOConfig

log = logging.getLogger(__name__)


class GTPOLossComputer:
    """Token-level clipped surrogate loss with per-turn constant advantages."""

    def __init__(
        self,
        config: GSPOConfig,
        dual_clip: bool = True,
        dual_clip_coef: float = 5.0,
        kl_coef: float | None = None,
    ) -> None:
        self.clip_low = config.clip_low
        self.clip_high = config.clip_high
        self.norm_adv_by_std = config.norm_adv_by_std
        self.dual_clip = dual_clip
        self.dual_clip_coef = dual_clip_coef
        self.kl_coef = kl_coef if kl_coef is not None else config.beta_kl

    @classmethod
    def from_config(cls, config: GSPOConfig) -> GTPOLossComputer:
        return cls(config=config)

    def __call__(
        self,
        log_probs: Tensor,
        old_log_probs: Tensor,
        advantages: Tensor,
        response_mask: Tensor,
        segment_ids: Tensor | None = None,
    ) -> dict[str, Tensor]:
        """Compute token-level clipped surrogate loss.

        segment_ids is accepted for API compatibility with GSPOLossComputer
        but not used — GTPO operates at token granularity.
        """
        log_ratios = log_probs - old_log_probs
        # Clamp log-ratios to prevent inf/NaN from degenerate tokens (Bug #2 fix)
        log_ratios = torch.clamp(log_ratios, min=-20.0, max=20.0)
        ratios = torch.exp(log_ratios)

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
