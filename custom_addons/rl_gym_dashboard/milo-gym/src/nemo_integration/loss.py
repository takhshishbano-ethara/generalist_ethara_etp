"""MiloGTPOLoss — NeMo-RL LossFunction protocol adapter for GTPO.

Wraps our existing GTPOLossComputer to conform to NeMo-RL's loss interface.
Handles the tensor slicing convention (data is [B,S], logprobs are [B,S-1]).
"""

from __future__ import annotations

import logging
from typing import Any

import torch
from torch import Tensor

from src.core.config import GSPOConfig
from src.training.gtpo_loss import GTPOLossComputer

log = logging.getLogger(__name__)

# Import NeMo-RL's actual enums for protocol compliance.
# NeMo-RL uses identity comparison — local copies won't match.
try:
    from nemo_rl.algorithms.loss.interfaces import LossInputType, LossType
except ImportError:
    # Fallback for environments without nemo_rl installed (testing, etc.)
    import enum

    class LossType(enum.Enum):  # type: ignore[no-redef]
        TOKEN_LEVEL = "token_level"
        SEQUENCE_LEVEL = "sequence_level"

    class LossInputType(enum.Enum):  # type: ignore[no-redef]
        LOGPROB = "logprob"
        LOGIT = "logit"


class MiloGTPOLoss:
    """NeMo-RL LossFunction protocol implementation using MILO's GTPO loss.

    NeMo-RL calls this as:
        loss_fn(data=batch, global_valid_seqs=N, global_valid_toks=M,
                next_token_logprobs=logprobs)

    We delegate to GTPOLossComputer which expects:
        (log_probs, old_log_probs, advantages, response_mask)
    """

    loss_type = LossType.TOKEN_LEVEL
    input_type = LossInputType.LOGPROB

    def __init__(self, config: GSPOConfig) -> None:
        self._computer = GTPOLossComputer.from_config(config)

    def __call__(
        self,
        data: dict[str, Any],
        global_valid_seqs: Tensor,
        global_valid_toks: Tensor,
        **kwargs: Any,
    ) -> tuple[Tensor, dict[str, Any]]:
        next_token_logprobs: Tensor = kwargs["next_token_logprobs"]  # [B, S-1]

        # NeMo-RL stores tensors at [B, S] (full seq_len).
        # next_token_logprobs is [B, S-1] (shifted). Slice data to match.
        token_mask = data["token_mask"][:, 1:]       # [B, S-1]
        sample_mask = data["sample_mask"]            # [B]
        advantages = data["advantages"][:, 1:]       # [B, S-1]
        # generation_logprobs = log-probs from the policy that GENERATED the data (behavior/old policy)
        # prev_logprobs = re-computed on current policy (new policy for ratio numerator)
        # PPO ratio = exp(new_logprobs - old_logprobs) = exp(next_token_logprobs - generation_logprobs)
        old_log_probs = data.get("generation_logprobs", data["prev_logprobs"])[:, 1:]  # [B, S-1]

        # Combined mask: token_mask AND sample_mask
        response_mask = token_mask * sample_mask.unsqueeze(-1)  # [B, S-1]

        result = self._computer(
            log_probs=next_token_logprobs,
            old_log_probs=old_log_probs,
            advantages=advantages,
            response_mask=response_mask,
        )

        loss = result["loss"]

        metrics = {
            "clip_frac": result["clip_frac"].item(),
            "approx_kl": result["approx_kl"].item(),
            "mean_ratio": result["mean_ratio"].item(),
            "kl_penalty": result["kl_penalty"].item(),
            "num_valid_samples": int(sample_mask.sum().item()),
            "num_valid_tokens": int(response_mask.sum().item()),
        }

        return loss, metrics
