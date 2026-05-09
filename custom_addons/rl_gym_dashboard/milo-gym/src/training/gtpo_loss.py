"""GTPO (Group Turn-level Policy Optimization) loss.

Token-level importance ratios with per-turn advantages from discounted returns.
Unlike GSPO's segment-level geometric-mean ratios, GTPO uses standard per-token
ratios w_t = exp(log_prob_t - old_log_prob_t) with asymmetric clipping and dual clip.

Entropy-weighted credit assignment concentrates gradient signal at high-uncertainty
decision points (tokens where the policy is most uncertain), improving long-horizon
multi-turn task performance.

Reference: arXiv:2511.14846, arXiv:2508.04349
"""

from __future__ import annotations

import logging

import torch
from torch import Tensor

from src.core.config import GSPOConfig

log = logging.getLogger(__name__)


class GTPOLossComputer:
    """Token-level clipped surrogate loss with entropy-weighted credit assignment."""

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
        self.gamma = config.gtpo_gamma
        self.ent_threshold = config.gtpo_ent_threshold
        self.ent_scale = config.gtpo_ent_scale

    @classmethod
    def from_config(cls, config: GSPOConfig) -> GTPOLossComputer:
        return cls(config=config)

    def _apply_entropy_credit(
        self,
        advantages: Tensor,
        log_probs: Tensor,
        response_mask: Tensor,
    ) -> Tensor:
        """Scale advantages at high-entropy tokens using -log_prob as entropy proxy."""
        token_entropy = -log_probs
        high_ent_mask = (token_entropy > self.ent_threshold) & (response_mask > 0)
        credit_scale = torch.ones_like(advantages)
        credit_scale[high_ent_mask] = (
            1.0 + self.ent_scale * (token_entropy[high_ent_mask] - self.ent_threshold)
        )
        return advantages * credit_scale

    def _apply_discounted_returns(
        self,
        advantages: Tensor,
        response_mask: Tensor,
        segment_ids: Tensor | None,
    ) -> Tensor:
        """Apply gamma-discounted returns within turn boundaries."""
        if segment_ids is None or self.gamma >= 1.0:
            return advantages

        result = advantages.clone()
        batch_size = advantages.shape[0]
        for b in range(batch_size):
            mask_b = response_mask[b] > 0
            if not mask_b.any():
                continue
            segs_b = segment_ids[b][mask_b]
            advs_b = advantages[b][mask_b]
            unique_segs = segs_b.unique()
            discounted = advs_b.clone()

            for seg in unique_segs:
                seg_mask = segs_b == seg
                seg_len = seg_mask.sum().item()
                if seg_len <= 1:
                    continue
                discount = torch.pow(
                    torch.tensor(self.gamma, device=advantages.device),
                    torch.arange(seg_len - 1, -1, -1, device=advantages.device, dtype=advantages.dtype)
                )
                discounted[seg_mask] = advs_b[seg_mask] * discount

            indices = mask_b.nonzero(as_tuple=True)[0]
            result[b, indices] = discounted

        return result

    def __call__(
        self,
        log_probs: Tensor,
        old_log_probs: Tensor,
        advantages: Tensor,
        response_mask: Tensor,
        segment_ids: Tensor | None = None,
    ) -> dict[str, Tensor]:
        """Compute token-level clipped surrogate loss with entropy credit."""
        log_ratios = log_probs - old_log_probs
        log_ratios = torch.clamp(log_ratios, min=-20.0, max=20.0)
        ratios = torch.exp(log_ratios)

        advantages = self._apply_discounted_returns(advantages, response_mask, segment_ids)
        advantages = self._apply_entropy_credit(advantages, log_probs, response_mask)

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
            ent_credit_frac = (
                ((-log_probs > self.ent_threshold).float() * response_mask).sum() / total_tokens
            )

        return {
            "loss": loss,
            "clip_frac": clip_frac,
            "approx_kl": approx_kl,
            "mean_ratio": mean_ratio,
            "kl_penalty": kl_penalty.squeeze(),
            "entropy_credit_frac": ent_credit_frac,
        }
