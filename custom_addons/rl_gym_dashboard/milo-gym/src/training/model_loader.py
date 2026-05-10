"""Model loading utilities: base model + LoRA + tokenizer + vLLM engine.

Handles the full model stack initialization for MILO-RL training:
- Loads base model with bfloat16 on training GPUs
- Applies PEFT LoRA (or loads from Stage 1 checkpoint)
- Loads tokenizer with proper padding config
- Creates vLLM offline engine for rollout generation
- Provides LoRA weight sync utility (HF model → vLLM engine)
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import torch

from src.core.config import MiloConfig

log = logging.getLogger(__name__)


@dataclass
class TrainingStack:
    """Container for all model components needed for training."""

    model: torch.nn.Module
    tokenizer: object  # PreTrainedTokenizerFast
    vllm_engine: object  # vllm.LLM
    model_name: str
    lora_adapter_path: str | None = None


def load_training_stack(
    config: MiloConfig,
    stage1_checkpoint_path: str | None = None,
    training_gpu_ids: list[int] | None = None,
    vllm_gpu_ids: list[int] | None = None,
) -> TrainingStack:
    """Load complete training stack: model + LoRA + tokenizer + vLLM.

    Args:
        config: Full MILO configuration.
        stage1_checkpoint_path: Path to Stage 1 LoRA adapter checkpoint.
            If provided, loads pre-trained adapter instead of fresh LoRA.
        training_gpu_ids: GPU IDs for training model (default: [0..training_gpus-1]).
        vllm_gpu_ids: GPU IDs for vLLM engine (default: last vllm_gpus GPUs).

    Returns:
        TrainingStack with model, tokenizer, and vLLM engine ready for training.
    """
    model_path = config.model_path
    hw = config.hardware
    lora_cfg = config.lora

    if training_gpu_ids is None:
        training_gpu_ids = list(range(hw.training_gpus))
    if vllm_gpu_ids is None:
        vllm_gpu_ids = list(range(hw.n_gpus - hw.vllm_gpus, hw.n_gpus))

    log.info("Loading training stack:")
    log.info("  Model: %s", model_path)
    log.info("  Training GPUs: %s", training_gpu_ids)
    log.info("  vLLM GPUs: %s", vllm_gpu_ids)
    log.info("  LoRA rank: %d, alpha: %d", lora_cfg.rank, lora_cfg.alpha)

    tokenizer = _load_tokenizer(model_path)

    model = _load_model_with_lora(
        model_path=model_path,
        lora_cfg=lora_cfg,
        stage1_checkpoint_path=stage1_checkpoint_path,
        training_gpu_ids=training_gpu_ids,
        moe_config=config.moe,
    )

    vllm_engine = _create_vllm_engine(
        model_path=model_path,
        config=config,
        vllm_gpu_ids=vllm_gpu_ids,
        lora_rank=lora_cfg.rank,
    )

    adapter_path = _sync_lora_to_vllm(model, vllm_engine, config)

    return TrainingStack(
        model=model,
        tokenizer=tokenizer,
        vllm_engine=vllm_engine,
        model_name=model_path,
        lora_adapter_path=adapter_path,
    )


def _load_tokenizer(model_path: str):
    """Load tokenizer with proper padding configuration."""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        model_path,
        trust_remote_code=True,
        padding_side="left",
    )

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id

    log.info("Tokenizer loaded: vocab_size=%d, pad_token=%s",
             tokenizer.vocab_size, tokenizer.pad_token)
    return tokenizer


def _load_model_with_lora(
    model_path: str,
    lora_cfg,
    stage1_checkpoint_path: str | None,
    training_gpu_ids: list[int],
    moe_config=None,
) -> torch.nn.Module:
    """Load base model and apply LoRA adapter."""
    from transformers import AutoModelForCausalLM

    if len(training_gpu_ids) == 1:
        device_map = {"": f"cuda:{training_gpu_ids[0]}"}
    else:
        device_map = "auto"
        os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in training_gpu_ids)

    log.info("Loading base model with device_map=%s...", device_map)
    base_model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        device_map=device_map,
        trust_remote_code=True,
        attn_implementation="flash_attention_2",
    )

    if moe_config and moe_config.freeze_router:
        _freeze_router_params(base_model)

    # Required by PEFT: gradient signal must flow through input embeddings
    base_model.enable_input_require_grads()

    if stage1_checkpoint_path and Path(stage1_checkpoint_path).exists():
        log.info("Loading Stage 1 LoRA adapter from: %s", stage1_checkpoint_path)
        from peft import PeftModel
        model = PeftModel.from_pretrained(
            base_model,
            stage1_checkpoint_path,
            is_trainable=True,
        )
    else:
        log.info("Initializing fresh LoRA adapter...")
        from peft import LoraConfig, get_peft_model, TaskType

        target_modules = [m.strip() for m in lora_cfg.target_modules.split(",") if m.strip()]
        modules_to_save = [m.strip() for m in lora_cfg.modules_to_save.split(",") if m.strip()] or None

        peft_config = LoraConfig(
            r=lora_cfg.rank,
            lora_alpha=lora_cfg.alpha,
            target_modules=target_modules,
            lora_dropout=lora_cfg.dropout,
            bias="none",
            task_type=TaskType.CAUSAL_LM,
            modules_to_save=modules_to_save,
        )
        model = get_peft_model(base_model, peft_config)

    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())
    log.info("Model loaded: trainable=%d (%.2f%%), total=%d",
             trainable_params, 100.0 * trainable_params / total_params, total_params)

    return model


def _freeze_router_params(model: torch.nn.Module) -> None:
    """Freeze MoE router parameters to prevent routing instability during RL."""
    frozen_count = 0
    for name, param in model.named_parameters():
        if "router" in name.lower() or "gate" in name.lower():
            param.requires_grad = False
            frozen_count += 1
    if frozen_count > 0:
        log.info("Frozen %d MoE router parameters", frozen_count)


def _create_vllm_engine(
    model_path: str,
    config: MiloConfig,
    vllm_gpu_ids: list[int],
    lora_rank: int,
):
    """Create vLLM offline engine for rollout generation."""
    from vllm import LLM

    hw = config.hardware

    vllm_devices = ",".join(str(g) for g in vllm_gpu_ids)

    log.info("Creating vLLM engine: tp=%d, max_model_len=%d, gpus=%s",
             hw.tp_size, hw.max_model_len, vllm_devices)

    engine = LLM(
        model=model_path,
        tensor_parallel_size=hw.tp_size,
        gpu_memory_utilization=hw.gpu_memory_utilization,
        max_model_len=hw.max_model_len,
        trust_remote_code=True,
        dtype="bfloat16",
        enable_lora=True,
        max_lora_rank=lora_rank,
        max_num_seqs=hw.max_concurrent_sequences,
    )

    log.info("vLLM engine created successfully")
    return engine


def _sync_lora_to_vllm(
    model: torch.nn.Module,
    vllm_engine,
    config: MiloConfig,
) -> str:
    """Sync LoRA adapter weights from HF model to vLLM engine.

    Strategy: Save adapter to temp directory, create LoRARequest for vLLM.
    This is the most portable approach (works without verl's monkey-patch).

    Returns:
        Path to the saved adapter directory.
    """
    from vllm.lora.request import LoRARequest

    adapter_dir = os.path.join(
        config.output_dir, config.run_id, "vllm_adapter_sync"
    )
    os.makedirs(adapter_dir, exist_ok=True)

    model.save_pretrained(adapter_dir)
    log.info("LoRA adapter saved for vLLM sync: %s", adapter_dir)

    return adapter_dir


def sync_lora_to_vllm(
    model: torch.nn.Module,
    vllm_engine,
    adapter_dir: str,
    lora_id: int = 1,
) -> str:
    """Public API: sync LoRA weights from training model to vLLM engine.

    Call this periodically during training (e.g., every 10 steps) to keep
    vLLM's rollout generation in sync with the trained adapter.

    Args:
        model: PEFT model with trained LoRA adapter.
        vllm_engine: vLLM LLM instance with enable_lora=True.
        adapter_dir: Directory to save adapter weights.
        lora_id: Unique ID for this LoRA adapter in vLLM.

    Returns:
        Path to saved adapter directory.
    """
    from vllm.lora.request import LoRARequest

    model.save_pretrained(adapter_dir)

    log.debug("LoRA weights synced to vLLM adapter dir: %s", adapter_dir)
    return adapter_dir


def get_lora_request(adapter_dir: str, lora_id: int = 1):
    """Create a vLLM LoRARequest for generation with the synced adapter.

    Use this when calling vllm_engine.generate() with LoRA:
        lora_req = get_lora_request(adapter_dir)
        outputs = engine.generate(prompts, sampling_params, lora_request=lora_req)
    """
    from vllm.lora.request import LoRARequest

    return LoRARequest(
        lora_name="milo_adapter",
        lora_int_id=lora_id,
        lora_path=adapter_dir,
    )


def compute_ref_log_probs(
    model: torch.nn.Module,
    input_ids: torch.Tensor,
    attention_mask: torch.Tensor,
) -> torch.Tensor:
    """Compute log-probs under reference policy (base model without LoRA).

    Uses PEFT's disable_adapter_layers() to temporarily remove LoRA,
    giving reference log-probs without extra memory for a separate model.

    Args:
        model: PEFT model (LoRA adapter active).
        input_ids: Token IDs [batch_size, seq_len].
        attention_mask: Attention mask [batch_size, seq_len].

    Returns:
        Per-token log-probs under reference policy [batch_size, seq_len-1].
    """
    model.eval()

    with torch.no_grad():
        # Disable LoRA to get base model (reference) log-probs
        model.base_model.disable_adapter_layers()
        try:
            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            logits = outputs.logits[:, :-1, :]  # Shift: predict next token
            target_ids = input_ids[:, 1:]  # Target: actual next tokens

            # Per-token log-probs via gather (efficient — no full log_softmax)
            log_probs = logits.log_softmax(dim=-1)
            ref_log_probs = log_probs.gather(2, target_ids.unsqueeze(-1)).squeeze(-1)
        finally:
            model.base_model.enable_adapter_layers()

    model.train()
    return ref_log_probs


def compute_current_log_probs(
    model: torch.nn.Module,
    input_ids: torch.Tensor,
    attention_mask: torch.Tensor,
) -> torch.Tensor:
    """Compute log-probs under current policy (with LoRA active).

    This IS differentiable — used in the training loss computation.

    Args:
        model: PEFT model (LoRA adapter active).
        input_ids: Token IDs [batch_size, seq_len].
        attention_mask: Attention mask [batch_size, seq_len].

    Returns:
        Per-token log-probs under current policy [batch_size, seq_len-1].
    """
    outputs = model(input_ids=input_ids, attention_mask=attention_mask)
    logits = outputs.logits[:, :-1, :]  # Shift: predict next token
    target_ids = input_ids[:, 1:]  # Target: actual next tokens

    log_probs = logits.log_softmax(dim=-1)
    current_log_probs = log_probs.gather(2, target_ids.unsqueeze(-1)).squeeze(-1)
    return current_log_probs
