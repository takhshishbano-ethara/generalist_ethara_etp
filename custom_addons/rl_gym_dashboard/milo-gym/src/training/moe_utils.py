"""Utilities for MoE-specific RL training.

Implements frozen router management (NVIDIA Nemotron-3 approach),
expert bias updates (aux-loss-free load balancing from DeepSeek/NVIDIA),
load balance monitoring, and expert collapse detection.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import torch
import torch.nn as nn

from src.core.config import MoEConfig

log = logging.getLogger(__name__)

_ROUTER_PATTERNS = ("router", "gate", "moe_gate", "routing")


def freeze_router_params(model: nn.Module) -> int:
    """Freeze all MoE router parameters. Returns count of frozen param tensors."""
    frozen_count = 0
    for name, param in model.named_parameters():
        if any(pattern in name for pattern in _ROUTER_PATTERNS):
            param.requires_grad = False
            frozen_count += 1
    if frozen_count > 0:
        log.info("Froze %d router parameters", frozen_count)
    else:
        log.warning(
            "No router parameters found to freeze. "
            "Model may not have MoE layers or uses non-standard naming."
        )
    return frozen_count


def update_expert_bias(
    model: nn.Module,
    routing_counts: torch.Tensor,
    target_ratio: float | None = None,
    update_rate: float = 0.001,
) -> int:
    """Update expert bias for aux-loss-free load balancing (DeepSeek/NVIDIA approach).

    Bias is applied additively to router logits before the sigmoid gate.
    Overloaded experts get increased bias (harder to select);
    underloaded experts get decreased bias (easier to select).

    Args:
        model: Model containing expert_bias parameters or router modules.
        routing_counts: Shape [num_experts] — tokens routed to each expert this step.
        target_ratio: Target fraction per expert. Defaults to 1/num_experts.
        update_rate: Step size for bias adjustment.

    Returns:
        Number of bias tensors updated.
    """
    num_experts = routing_counts.shape[0]
    if target_ratio is None:
        target_ratio = 1.0 / num_experts

    total_tokens = routing_counts.sum()
    if total_tokens == 0:
        return 0

    actual_ratios = routing_counts.float() / total_tokens
    imbalance = actual_ratios - target_ratio
    bias_delta = imbalance * update_rate

    updated_count = 0
    for _name, buf in _iter_expert_bias_buffers(model):
        if buf.shape[0] == num_experts:
            buf.add_(bias_delta.to(buf.device, dtype=buf.dtype))
            updated_count += 1
        elif buf.numel() == num_experts:
            buf.view(-1).add_(bias_delta.to(buf.device, dtype=buf.dtype))
            updated_count += 1

    if updated_count == 0:
        updated_count = _inject_expert_bias(model, bias_delta)

    return updated_count


def _iter_expert_bias_buffers(model: nn.Module):
    """Yield (name, tensor) for all expert bias buffers/parameters in the model."""
    bias_patterns = ("expert_bias", "routing_bias", "gate_bias", "moe_bias")
    for name, param in model.named_parameters():
        if any(p in name for p in bias_patterns):
            yield name, param.data
    for name, buf in model.named_buffers():
        if any(p in name for p in bias_patterns):
            yield name, buf


def _inject_expert_bias(model: nn.Module, bias_delta: torch.Tensor) -> int:
    """Inject expert bias into router Linear modules lacking explicit bias buffers."""
    updated = 0
    for name, module in model.named_modules():
        if not any(pattern in name for pattern in _ROUTER_PATTERNS):
            continue
        if not isinstance(module, nn.Linear):
            continue
        if module.out_features != bias_delta.shape[0]:
            continue

        if module.bias is not None:
            module.bias.data.add_(bias_delta.to(module.bias.device, dtype=module.bias.dtype))
        else:
            bias_buf = bias_delta.clone().to(
                next(module.parameters()).device,
                dtype=next(module.parameters()).dtype,
            )
            module.register_buffer("expert_bias", bias_buf)
        updated += 1
    return updated


def compute_load_balance_stats(routing_counts: torch.Tensor) -> dict[str, float | int]:
    """Compute load balance statistics for expert utilization monitoring.

    Returns dict with: max_load_ratio, min_load_ratio, coefficient_of_variation,
    dead_experts (0 tokens), collapsed_experts (< 1% of mean).
    """
    counts = routing_counts.float()
    mean_count = counts.mean()

    if mean_count == 0:
        num_experts = counts.shape[0]
        return {
            "max_load_ratio": 0.0,
            "min_load_ratio": 0.0,
            "coefficient_of_variation": 0.0,
            "dead_experts": num_experts,
            "collapsed_experts": num_experts,
        }

    max_load_ratio = (counts.max() / mean_count).item()
    min_load_ratio = (counts.min() / mean_count).item()
    cv = counts.std().item() / mean_count.item()
    dead_experts = int((counts == 0).sum().item())
    collapsed_experts = int((counts < 0.01 * mean_count).sum().item())

    return {
        "max_load_ratio": max_load_ratio,
        "min_load_ratio": min_load_ratio,
        "coefficient_of_variation": cv,
        "dead_experts": dead_experts,
        "collapsed_experts": collapsed_experts,
    }


def check_expert_collapse(
    routing_counts: torch.Tensor,
    collapse_threshold: float = 0.01,
) -> tuple[bool, list[int]]:
    """Detect if any expert receives < collapse_threshold * mean(counts) tokens.

    Returns:
        (is_collapsed, list_of_collapsed_expert_ids)
    """
    counts = routing_counts.float()
    mean_count = counts.mean()

    if mean_count == 0:
        collapsed_ids = list(range(counts.shape[0]))
        return True, collapsed_ids

    threshold = collapse_threshold * mean_count
    collapsed_mask = counts < threshold
    raw_ids = collapsed_mask.nonzero(as_tuple=False).squeeze(-1).tolist()

    collapsed_ids: list[int]
    if isinstance(raw_ids, int):
        collapsed_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        collapsed_ids = [int(x) for x in raw_ids]
    else:
        collapsed_ids = []

    return len(collapsed_ids) > 0, collapsed_ids


def compute_seq_aux_loss(
    routing_logits: torch.Tensor,
    expert_indices: torch.Tensor,
    num_experts: int,
    top_k: int,
) -> torch.Tensor:
    """Compute sequence-level auxiliary load balancing loss (Nemotron/DeepSeek).

    Loss = mean_over_batch[ num_experts * sum_i(f_i * P_i) ]
    where f_i = fraction of tokens routed to expert i per sequence,
          P_i = mean sigmoid routing probability for expert i per sequence.

    Args:
        routing_logits: [batch, seq_len, num_experts] — raw router outputs.
        expert_indices: [batch, seq_len, top_k] — selected expert indices.
        num_experts: Total number of routable experts.
        top_k: Number of experts selected per token.

    Returns:
        Scalar auxiliary loss tensor.
    """
    batch_size, seq_len, _ = routing_logits.shape

    routing_probs = torch.sigmoid(routing_logits)

    expert_mask = torch.zeros(
        batch_size, seq_len, num_experts,
        device=routing_logits.device,
        dtype=routing_logits.dtype,
    )
    for k in range(top_k):
        expert_mask.scatter_(2, expert_indices[:, :, k].unsqueeze(-1), 1.0)

    # f_i: token fraction per expert per sequence [B, E]
    f = expert_mask.sum(dim=1) / (seq_len * top_k)

    # P_i: mean routing probability per expert per sequence [B, E]
    p = routing_probs.mean(dim=1)

    # seq_aux_loss = num_experts * sum_i(f_i * P_i), averaged over batch
    return ((f * p).sum(dim=-1) * num_experts).mean()


class MoETrainingManager:
    """Encapsulates MoE-specific training logic: router freezing, bias updates,
    load monitoring, and collapse detection."""

    def __init__(self, config: MoEConfig) -> None:
        self.config = config
        self._step_count: int = 0
        self._router_frozen: bool = False
        self._frozen_param_count: int = 0
        self._load_history: deque[dict[str, Any]] = deque(maxlen=100)
        self._collapse_events: list[dict[str, Any]] = []
        self._cumulative_counts: torch.Tensor | None = None

    def setup(self, model: nn.Module) -> dict[str, Any]:
        """Freeze router if configured, return setup summary dict."""
        setup_info: dict[str, Any] = {
            "num_experts": self.config.num_experts,
            "num_shared_experts": self.config.num_shared_experts,
            "top_k": self.config.top_k,
            "freeze_router": self.config.freeze_router,
            "aux_loss_coeff": self.config.aux_loss_coeff,
            "expert_bias_update_rate": self.config.expert_bias_update_rate,
            "router_load_balancing_type": self.config.router_load_balancing_type,
        }

        if self.config.freeze_router:
            self._frozen_param_count = freeze_router_params(model)
            self._router_frozen = True
            setup_info["frozen_params"] = self._frozen_param_count
        else:
            setup_info["frozen_params"] = 0

        log.info(
            "MoE training setup: %d experts, top-%d, router %s",
            self.config.num_experts,
            self.config.top_k,
            "frozen" if self._router_frozen else "trainable",
        )
        return setup_info

    def step(
        self,
        model: nn.Module,
        routing_counts: torch.Tensor,
    ) -> dict[str, Any]:
        """Execute one MoE step: update bias, check health, return stats dict."""
        self._step_count += 1
        stats: dict[str, Any] = {"moe_step": self._step_count}

        if self._cumulative_counts is None:
            self._cumulative_counts = routing_counts.clone().float()
        else:
            self._cumulative_counts.add_(routing_counts.float().to(self._cumulative_counts.device))

        if self.config.freeze_router:
            bias_updates = update_expert_bias(
                model=model,
                routing_counts=routing_counts,
                target_ratio=1.0 / self.config.num_experts,
                update_rate=self.config.expert_bias_update_rate,
            )
            stats["bias_updates_applied"] = bias_updates

        load_stats = compute_load_balance_stats(routing_counts)
        stats.update(load_stats)
        self._load_history.append(load_stats)

        is_collapsed, collapsed_ids = check_expert_collapse(routing_counts, collapse_threshold=0.01)
        stats["is_collapsed"] = is_collapsed
        stats["collapsed_expert_ids"] = collapsed_ids

        if is_collapsed:
            self._collapse_events.append({
                "step": self._step_count,
                "collapsed_ids": collapsed_ids,
                "num_collapsed": len(collapsed_ids),
            })
            log.warning(
                "Expert collapse at step %d: %d experts (ids: %s)",
                self._step_count,
                len(collapsed_ids),
                collapsed_ids[:10],
            )

        return stats

    def get_aux_loss(
        self,
        routing_logits: torch.Tensor,
        expert_indices: torch.Tensor,
    ) -> torch.Tensor:
        """Compute seq_aux_loss scaled by aux_loss_coeff. Returns 0.0 if disabled."""
        if self.config.aux_loss_coeff == 0.0:
            return torch.tensor(0.0, device=routing_logits.device, dtype=routing_logits.dtype)

        if self.config.router_load_balancing_type != "seq_aux_loss":
            return torch.tensor(0.0, device=routing_logits.device, dtype=routing_logits.dtype)

        raw_loss = compute_seq_aux_loss(
            routing_logits=routing_logits,
            expert_indices=expert_indices,
            num_experts=self.config.num_experts,
            top_k=self.config.top_k,
        )
        return self.config.aux_loss_coeff * raw_loss

    def state_dict(self) -> dict[str, Any]:
        """Serialize manager state for checkpointing."""
        state: dict[str, Any] = {
            "step_count": self._step_count,
            "router_frozen": self._router_frozen,
            "frozen_param_count": self._frozen_param_count,
            "collapse_events": self._collapse_events,
            "load_history": list(self._load_history),
        }
        if self._cumulative_counts is not None:
            state["cumulative_counts"] = self._cumulative_counts.cpu()
        return state

    def load_state_dict(self, state: dict[str, Any]) -> None:
        """Restore manager state from checkpoint."""
        self._step_count = state.get("step_count", 0)
        self._router_frozen = state.get("router_frozen", False)
        self._frozen_param_count = state.get("frozen_param_count", 0)
        self._collapse_events = state.get("collapse_events", [])
        self._load_history = deque(state.get("load_history", []), maxlen=100)

        cumulative = state.get("cumulative_counts")
        self._cumulative_counts = cumulative.clone() if cumulative is not None else None
