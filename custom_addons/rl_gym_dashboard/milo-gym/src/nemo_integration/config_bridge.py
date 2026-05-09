"""Config bridge: translates MILO config dataclasses to NeMo-RL's expected dict format."""

from __future__ import annotations

from typing import Any

from src.core.config import (
    CurriculumConfig,
    GSPOConfig,
    GatedRewardConfig,
    HardwareConfig,
    LoRAConfig,
    MoEConfig,
    MonitoringConfig,
    PRMConfig,
)


def build_nemo_master_config(
    gspo: GSPOConfig,
    hardware: HardwareConfig,
    lora: LoRAConfig,
    curriculum: CurriculumConfig,
    prm: PRMConfig,
    gated_reward: GatedRewardConfig,
    monitoring: MonitoringConfig,
    moe: MoEConfig | None = None,
    model_name: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-Base-BF16",
    tokenizer_name: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
) -> dict[str, Any]:
    """Build NeMo-RL MasterConfig dict from MILO config dataclasses."""

    return {
        "grpo": {
            "num_prompts_per_step": gspo.batch_size // gspo.group_size,
            "num_generations_per_prompt": gspo.group_size,
            "max_rollout_turns": curriculum.phases[-1].max_turns if curriculum.phases else 50,
            "adv_estimator": {
                "name": "milo_step",
                "prm_config": {
                    "advantage_mode": prm.advantage_mode,
                    "min_group_variance": prm.min_group_variance,
                    "gtpo_gamma": prm.gtpo_gamma,
                },
                "group_size": gspo.group_size,
            },
            "reward_scaling": {
                "enabled": False,
            },
            "reward_shaping": {
                "enabled": False,
            },
            "use_leave_one_out_baseline": True,
        },
        "loss_fn": {
            "name": "milo_gtpo",
            "clip_low": gspo.clip_low,
            "clip_high": gspo.clip_high,
            "dual_clip": True,
            "dual_clip_coef": 5.0,
            "kl_coef": gspo.beta_kl,
            "norm_adv_by_std": gspo.norm_adv_by_std,
            "token_level_loss": True,
        },
        "policy": {
            "model_name": model_name,
            "tokenizer": {"name": tokenizer_name},
            "train_global_batch_size": gspo.batch_size,
            "train_micro_batch_size": gspo.micro_batch_size,
            "logprob_batch_size": gspo.micro_batch_size,
            "max_total_sequence_length": hardware.max_model_len,
            "dtensor_cfg": {
                "enabled": True,
                "tensor_parallel_size": hardware.tp_size,
                "sequence_parallel": True,
                "automodel_kwargs": {"force_hf": True},
                "lora_cfg": {
                    "enabled": True,
                    "dim": lora.rank,
                    "alpha": lora.alpha,
                    "exclude_modules": ["*out_proj*"],
                    "match_all_linear": False,
                    "dropout": lora.dropout,
                    "dropout_position": "post",
                    "lora_A_init": "xavier",
                    "use_triton": False,
                },
            },
            "optimizer": {
                "name": "adamw",
                "lr": gspo.learning_rate,
                "weight_decay": 0.01,
                "betas": [0.9, 0.999],
                "grad_clip": gspo.max_grad_norm,
            },
            "scheduler": {
                "name": "cosine",
                "warmup_steps": gspo.warmup_steps,
                "total_steps": gspo.total_steps,
                "min_lr_ratio": 0.1,
            },
            "generation": {
                "backend": "vllm",
                "vllm_cfg": {
                    "tensor_parallel_size": hardware.tp_size,
                    "max_model_len": hardware.max_model_len,
                    "gpu_memory_utilization": hardware.gpu_memory_utilization,
                },
                "temperature": gspo.temperature,
                "top_p": gspo.top_p,
                "max_new_tokens": 4096,
                "colocated": {
                    "enabled": True,
                },
            },
            "sequence_packing": {"enabled": False},
        },
        "data": {
            "train": {"dataset_name": "milo_curriculum"},
            "default": {"env_name": "milo_docker"},
        },
        "env": {
            "milo_docker": {
                "max_concurrent_containers": hardware.docker_containers,
                "timeout_seconds": hardware.docker_timeout,
                "prm_mode": prm.mode,
                "gated_reward_config": {
                    "gate_threshold": gated_reward.gate_threshold,
                    "outcome_pass": gated_reward.outcome_pass,
                    "outcome_fail": gated_reward.outcome_fail,
                    "outcome_empty": gated_reward.outcome_empty,
                    "outcome_timeout": gated_reward.outcome_timeout,
                    "prm_weight": gated_reward.prm_weight,
                    "length_penalty_weight": gated_reward.length_penalty_weight,
                },
                "prm_config": {
                    "mode": prm.mode,
                    "prm_alpha": prm.prm_alpha,
                    "gtpo_gamma": prm.gtpo_gamma,
                    "advantage_mode": prm.advantage_mode,
                    "min_group_variance": prm.min_group_variance,
                },
            },
        },
        "checkpointing": {
            "checkpoint_dir": "results/milo-nemo-rl",
            "save_every_n_steps": monitoring.checkpoint_every,
            "max_to_keep": monitoring.keep_checkpoints,
        },
        "cluster": {
            "gpus_per_node": hardware.n_gpus,
            "num_nodes": 1,
        },
        "logger": {
            "wandb_enabled": monitoring.use_wandb,
            "tensorboard_enabled": True,
            "wandb": {
                "project": monitoring.wandb_project,
                "name": "milo-nemo-rl",
            },
        },
    }
