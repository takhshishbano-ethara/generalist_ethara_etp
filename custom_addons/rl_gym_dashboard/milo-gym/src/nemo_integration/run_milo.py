"""MILO NeMo-RL training entry point.

Monkey-patches NeMo-RL's GRPO loop with:
- MiloGTPOLoss (custom GTPO loss function)
- MiloAdvantageEstimator (per-token PRM step advantages)
- MiloDockerEnvironment (Docker sandbox multi-turn environment)
- MiloCurriculumDataloader (4-phase curriculum sampler)

Usage:
    python -m src.nemo_integration.run_milo \
        --config configs/nemo_grpo_milo.yaml \
        --milo-config configs/ppo_trainer.yaml \
        --tasks data/tasks.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import torch
import yaml

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MILO NeMo-RL Training")
    parser.add_argument(
        "--config",
        type=str,
        default="configs/nemo_grpo_milo.yaml",
        help="Path to NeMo-RL recipe YAML",
    )
    parser.add_argument(
        "--milo-config",
        type=str,
        default="configs/ppo_trainer.yaml",
        help="Path to MILO config YAML (for curriculum, PRM, gated rewards)",
    )
    parser.add_argument(
        "--tasks",
        type=str,
        default="data/tasks.jsonl",
        help="Path to task registry JSONL",
    )
    parser.add_argument(
        "--stage1-checkpoint",
        type=str,
        default=None,
        help="Path to Stage 1 RFT checkpoint (LoRA adapter)",
    )
    parser.add_argument(
        "--resume-from",
        type=str,
        default=None,
        help="Path to checkpoint to resume training from",
    )
    return parser.parse_args()


def load_task_registry(tasks_path: str) -> dict[int, Any]:
    from src.core.schemas import TaskSpec

    registry: dict[int, TaskSpec] = {}
    path = Path(tasks_path)

    if not path.exists():
        log.error("Tasks file not found: %s", tasks_path)
        sys.exit(1)

    with open(path) as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            task = TaskSpec(**data)
            registry[idx] = task

    log.info("Loaded %d tasks from %s", len(registry), tasks_path)
    return registry


def build_task_difficulties(registry: dict[int, Any]) -> dict[int, str]:
    return {idx: task.difficulty for idx, task in registry.items()}


def patch_advantage_factory(prm_config: Any, group_size: int) -> None:
    """Monkey-patch NeMo-RL's advantage estimator factory."""
    try:
        import nemo_rl.algorithms.grpo as grpo_module
    except ImportError:
        log.warning("nemo_rl not installed — skipping advantage factory patch")
        return

    from src.nemo_integration.advantage import MiloAdvantageEstimator

    original_create = grpo_module._create_advantage_estimator

    def patched_create(master_config: dict) -> Any:
        adv_name = master_config.get("grpo", {}).get("adv_estimator", {}).get("name", "")
        if adv_name == "milo_step":
            return MiloAdvantageEstimator(prm_config, group_size)
        return original_create(master_config)

    grpo_module._create_advantage_estimator = patched_create
    log.info("Patched _create_advantage_estimator for milo_step mode")


def patch_post_rollout(environment: Any) -> None:
    """Monkey-patch run_multi_turn_rollout to call global_post_process_and_metrics.

    NeMo-RL's GRPO loop at commit 7dd5d90 NEVER calls global_post_process_and_metrics().
    Our reward pipeline (Docker grading, PRM scoring, partial credit, format penalties,
    gated rewards, turn_spans) lives in that method. This patch injects it after rollout
    completes, overwriting the 0.0 total_reward with properly shaped rewards.
    """
    try:
        import nemo_rl.experience.rollouts as rollouts_module
    except ImportError:
        log.warning("nemo_rl not installed — skipping post-rollout patch")
        return

    original_run_rollout = rollouts_module.run_multi_turn_rollout
    original_run_async_rollout = getattr(rollouts_module, "run_async_multi_turn_rollout", None)

    def patched_run_rollout(*args, **kwargs):
        # run_multi_turn_rollout returns tuple[BatchedDataDict, dict]
        batch, rollout_metrics = original_run_rollout(*args, **kwargs)
        try:
            # Call post-processing locally (not .remote()) to avoid GPU tensor
            # serialization issues through Ray. The environment actor handles
            # Docker/PRM calls but operates on CPU-accessible data only.
            batch_result, post_metrics = environment.global_post_process_and_metrics(batch)
            if post_metrics:
                log.info(
                    "Post-rollout: success_rate=%.3f, avg_turns=%.1f, format_violations=%d",
                    post_metrics.get("env/success_rate", 0),
                    post_metrics.get("env/avg_turns", 0),
                    int(post_metrics.get("env/format_violations", 0)),
                )
            return batch_result, {**rollout_metrics, **post_metrics}
        except Exception as e:
            log.error("global_post_process_and_metrics failed: %s", e)
            return batch, rollout_metrics

    rollouts_module.run_multi_turn_rollout = patched_run_rollout

    if original_run_async_rollout is not None:
        def patched_async_rollout(*args, **kwargs):
            batch, rollout_metrics = original_run_async_rollout(*args, **kwargs)
            try:
                batch_result, post_metrics = environment.global_post_process_and_metrics(batch)
                if post_metrics:
                    log.info(
                        "Post-rollout (async): success_rate=%.3f, avg_turns=%.1f",
                        post_metrics.get("env/success_rate", 0),
                        post_metrics.get("env/avg_turns", 0),
                    )
                return batch_result, {**rollout_metrics, **post_metrics}
            except Exception as e:
                log.error("global_post_process_and_metrics (async) failed: %s", e)
                return batch, rollout_metrics

        rollouts_module.run_async_multi_turn_rollout = patched_async_rollout

    log.info("Patched run_multi_turn_rollout with post-rollout reward processing")


def build_loss_fn(nemo_config: dict) -> Any:
    from src.core.config import GSPOConfig
    from src.nemo_integration.loss import MiloGTPOLoss

    loss_cfg = nemo_config.get("loss_fn", {})

    gspo_config = GSPOConfig(
        clip_low=loss_cfg.get("clip_low", 3e-4),
        clip_high=loss_cfg.get("clip_high", 4e-4),
        beta_kl=loss_cfg.get("kl_coef", 0.0),
        norm_adv_by_std=loss_cfg.get("norm_adv_by_std", True),
    )

    return MiloGTPOLoss(gspo_config)


def build_environment(env_config: dict) -> tuple[Any, Any]:
    """Build environment. Returns (ray_env_for_step, local_env_for_post_process).

    env.step() must be a Ray actor (called with .remote() by NeMo-RL).
    global_post_process_and_metrics() is called locally to avoid GPU tensor serialization.
    """
    from src.nemo_integration.environment import MiloDockerEnvironment

    local_env = MiloDockerEnvironment(env_config)

    try:
        import ray
        RemoteEnv = ray.remote(max_restarts=-1, max_task_retries=-1)(MiloDockerEnvironment)
        ray_env = RemoteEnv.remote(env_config)
        return ray_env, local_env
    except ImportError:
        log.warning("Ray not installed — using local environment (no parallelism)")
        return local_env, local_env


def build_dataloader(
    nemo_config: dict,
    task_registry: dict[int, Any],
    tokenizer: Any,
    curriculum_config: Any,
) -> Any:
    from src.nemo_integration.dataloader import MiloCurriculumDataloader
    from src.training.curriculum import ScalingInterRLSampler

    task_difficulties = build_task_difficulties(task_registry)
    sampler = ScalingInterRLSampler(
        task_difficulties=task_difficulties,
        curriculum_config=curriculum_config,
    )

    num_prompts = nemo_config.get("grpo", {}).get("num_prompts_per_step", 8)

    return MiloCurriculumDataloader(
        sampler=sampler,
        task_registry=task_registry,
        tokenizer=tokenizer,
        num_prompts_per_step=num_prompts,
    )


def _build_dummy_dataset(tokenizer: Any) -> Any:
    """Build minimal dataset to satisfy NeMo-RL setup() requirements."""
    try:
        from nemo_rl.data.datasets import AllTaskProcessedDataset
    except ImportError:
        return [{"message_log": [{"role": "user", "content": "dummy"}], "length": 5}]

    dummy_data = [
        {
            "message_log": [
                {"role": "user", "content": "Fix the bug in main.py"}
            ],
            "length": 10,
            "task_name": "milo_docker",
        }
    ]
    try:
        return AllTaskProcessedDataset(dummy_data, tokenizer)
    except Exception:
        return dummy_data


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    args = parse_args()

    # Load NeMo-RL config
    with open(args.config) as f:
        nemo_config: dict = yaml.safe_load(f)

    # Load MILO config
    milo_config_path = Path(args.milo_config)
    if milo_config_path.exists():
        with open(milo_config_path) as f:
            milo_raw: dict = yaml.safe_load(f) or {}
    else:
        milo_raw = {}

    # Load task registry
    task_registry = load_task_registry(args.tasks)

    # Build MILO config objects
    from src.core.config import CurriculumConfig, GSPOConfig, PRMConfig

    gspo_config = GSPOConfig(**milo_raw.get("gspo", milo_raw.get("training", {})))
    prm_config = PRMConfig(**milo_raw.get("prm", {}))
    curriculum_config = CurriculumConfig(**milo_raw.get("curriculum", {}))

    # group_size must match num_generations_per_prompt (NOT gspo_config.group_size
    # which may differ from the NeMo-RL recipe). NeMo-RL repeats each prompt N times,
    # so groups are contiguous blocks of N.
    nemo_group_size = nemo_config.get("grpo", {}).get("num_generations_per_prompt", 8)
    patch_advantage_factory(prm_config, nemo_group_size)

    loss_fn = build_loss_fn(nemo_config)
    log.info("Built MiloGTPOLoss (clip_low=%.4f, clip_high=%.4f)", gspo_config.clip_low, gspo_config.clip_high)

    env_config = nemo_config.get("env", {}).get("milo_docker", {})
    ray_environment, local_environment = build_environment(env_config)
    task_to_env = {"milo_docker": ray_environment}
    log.info("Built MiloDockerEnvironment")

    patch_post_rollout(local_environment)

    # Initialize NeMo-RL
    try:
        from nemo_rl.algorithms.grpo import grpo_train, setup
        from nemo_rl.algorithms.utils import get_tokenizer
        from nemo_rl.distributed.virtual_cluster import init_ray
        from nemo_rl.models.generation import configure_generation_config
    except ImportError as e:
        log.error(
            "NeMo-RL not installed. Install with: pip install nemo-rl>=0.6.0\n"
            "Error: %s", e
        )
        sys.exit(1)

    init_ray()

    # Get tokenizer
    tokenizer_cfg = nemo_config.get("policy", {}).get("tokenizer", {})
    tokenizer = get_tokenizer(tokenizer_cfg)

    # Configure generation
    nemo_config["policy"]["generation"] = configure_generation_config(
        nemo_config["policy"]["generation"], tokenizer
    )

    # Build curriculum dataloader
    dataloader = build_dataloader(nemo_config, task_registry, tokenizer, curriculum_config)
    log.info("Built MiloCurriculumDataloader (phase=%d, max_turns=%d)",
             dataloader.current_phase, dataloader.max_turns)

    # Build a dummy dataset for setup() — we replace the dataloader afterward
    # but setup() needs a non-None dataset to construct policy/generation/clusters.
    dummy_dataset = _build_dummy_dataset(tokenizer)

    (
        policy,
        policy_generation,
        _clusters,  # (train_cluster, inference_cluster)
        _dataloader,  # NeMo-RL's default — we replace it
        _val_dataloader,
        _loss_fn,  # NeMo-RL's default — we replace it
        logger,
        checkpointer,
        grpo_save_state,
        master_config,
    ) = setup(nemo_config, tokenizer, dataset=dummy_dataset, val_dataset=None)

    # Run GRPO training with our custom components
    log.info("Starting MILO NeMo-RL training (total_steps=%d)", gspo_config.total_steps)

    grpo_train(
        policy=policy,
        policy_generation=policy_generation,
        wrapped_dataloader=dataloader,
        val_dataloader=None,
        tokenizer=tokenizer,
        loss_fn=loss_fn,
        task_to_env=task_to_env,
        val_task_to_env=None,
        logger=logger,
        checkpointer=checkpointer,
        grpo_save_state=grpo_save_state,
        master_config=master_config,
    )

    log.info("Training complete.")


if __name__ == "__main__":
    main()
