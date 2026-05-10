"""Stage 2: GRPO reinforcement learning training."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import ECRConfig, load_milo_config
from src.data.dataset import MiloDataset
from src.rollout.docker_executor import DockerExecutor
from src.rollout.docker_tool import DockerSandboxTool
from src.training.curriculum import ScalingInterRLSampler
from src.training.reward_manager import MiloRewardManager
from src.training.trainer import MiloTrainer

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2: GRPO Training")
    parser.add_argument("--config", type=str, default="configs/ppo_trainer.yaml")
    parser.add_argument("--hardware", type=str, default="configs/hardware/8xh100.yaml")
    parser.add_argument("--data-dir", type=str, default="data/tasks/")
    parser.add_argument("--checkpoint", type=str, default=None, help="Resume from checkpoint")
    parser.add_argument("--stage1-handoff", type=str, default=None,
                        help="Path to stage1_handoff.json for checkpoint continuity")
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--no-vllm", action="store_true", help="Skip vLLM/model loading (dry run)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    config = load_milo_config(args.config)
    if args.run_id:
        config.run_id = args.run_id

    dataset = MiloDataset.from_jsonl(Path(args.data_dir) / "train.jsonl")
    log.info(f"Loaded {len(dataset)} training tasks")
    log.info(f"  Difficulty: {dataset.difficulty_distribution()}")
    log.info(f"  Language: {dataset.language_distribution()}")

    task_difficulties = {}
    for i, task in enumerate(dataset.tasks):
        task_difficulties[i] = task.difficulty

    task_registry = {t.task_id: t for t in dataset.tasks}
    task_id_map = {i: task.task_id for i, task in enumerate(dataset.tasks)}

    ecr = config.ecr
    if args.hardware:
        import yaml
        hw_path = Path(args.hardware)
        if hw_path.exists():
            with hw_path.open() as f:
                hw = yaml.safe_load(f)
            ecr_raw = hw.get("ecr", {})
            if ecr_raw:
                ecr = ECRConfig(**{k: v for k, v in ecr_raw.items() if k in ECRConfig.__dataclass_fields__})

    executor = DockerExecutor(
        max_concurrent=config.hardware.docker_containers,
        timeout=config.hardware.docker_timeout,
        cpu_limit=config.hardware.docker_cpu_per_container,
        mem_limit=config.hardware.docker_mem_per_container,
        ecr_config=ecr if ecr.enabled else None,
    )

    curriculum = ScalingInterRLSampler(
        task_difficulties=task_difficulties,
        curriculum_config=config.curriculum,
    )

    stage1_checkpoint_path = _resolve_stage1_checkpoint(args, config)

    model = None
    tokenizer = None
    vllm_engine = None
    rollout_engine = None
    lora_adapter_dir = None

    if not args.no_vllm:
        from src.training.model_loader import load_training_stack
        stack = load_training_stack(
            config,
            stage1_checkpoint_path=stage1_checkpoint_path,
        )
        model = stack.model
        tokenizer = stack.tokenizer
        vllm_engine = stack.vllm_engine
        lora_adapter_dir = stack.lora_adapter_path

        docker_tool = DockerSandboxTool(
            executor=executor,
            task_registry=task_registry,
            timeout_per_action=60,
        )

        from src.rollout.multi_turn_engine import MultiTurnRolloutEngine
        rollout_engine = MultiTurnRolloutEngine(
            vllm_engine=vllm_engine,
            docker_tool=docker_tool,
            tokenizer=tokenizer,
            config=config,
        )

        if lora_adapter_dir:
            from src.training.model_loader import get_lora_request
            rollout_engine.set_lora_request(get_lora_request(lora_adapter_dir))

    reward_manager = MiloRewardManager(
        tokenizer=tokenizer,
        executor=executor,
        task_registry=task_registry,
        compact_filtering=config.gspo.compact_filtering,
        max_resp_len=config.hardware.max_model_len,
    )

    eval_tasks = _load_eval_tasks(args.data_dir)

    trainer = MiloTrainer(
        config=config,
        reward_manager=reward_manager,
        curriculum=curriculum,
        model=model,
        tokenizer=tokenizer,
        vllm_engine=vllm_engine,
        rollout_engine=rollout_engine,
        lora_adapter_dir=lora_adapter_dir,
        eval_tasks=eval_tasks,
        task_id_map=task_id_map,
    )

    if args.checkpoint:
        log.info(f"Resuming from checkpoint: {args.checkpoint}")
        trainer._load_checkpoint(args.checkpoint)

    log.info(f"Starting GSPO training for {config.gspo.total_steps} steps")
    log.info(f"  Curriculum phases: {len(config.curriculum.phases)}")
    log.info(f"  Group size: {config.gspo.group_size}, Batch: {config.gspo.batch_size}")
    log.info(f"  Model loaded: {model is not None}")
    log.info(f"  Rollout engine: {'multi-turn' if rollout_engine else 'placeholder'}")

    trainer.fit()

    if trainer.is_stopped:
        log.warning(f"Training stopped: {trainer.stop_reason}")
    else:
        log.info("Training completed successfully")

    log.info(f"Best eval: {trainer._best_eval:.4f} at {trainer._best_checkpoint_path}")


def _resolve_stage1_checkpoint(args, config) -> str | None:
    """Find Stage 1 checkpoint from args or auto-detect from output directory."""
    if args.stage1_handoff:
        return _read_handoff(Path(args.stage1_handoff))

    default_handoff = Path(config.output_dir) / "stage1_handoff.json"
    if default_handoff.exists():
        return _read_handoff(default_handoff)

    return None


def _read_handoff(handoff_path: Path) -> str | None:
    if not handoff_path.exists():
        log.warning(f"Stage 1 handoff file not found: {handoff_path}")
        return None

    import json
    with handoff_path.open() as f:
        handoff = json.load(f)

    stage1_ckpt = handoff.get("checkpoint_path")
    if stage1_ckpt and Path(stage1_ckpt).exists():
        log.info(f"Loading Stage 1 checkpoint: {stage1_ckpt}")
        log.info(f"  Gate pass rate: {handoff.get('gate_pass_rate', 'unknown')}")
        return stage1_ckpt
    else:
        log.warning(f"Stage 1 checkpoint not found: {stage1_ckpt}")
        return None


def _load_eval_tasks(data_dir: str) -> list:
    """Load eval tasks from eval.jsonl if it exists."""
    eval_path = Path(data_dir) / "eval.jsonl"
    if not eval_path.exists():
        return []
    try:
        eval_dataset = MiloDataset.from_jsonl(eval_path)
        log.info(f"Loaded {len(eval_dataset)} eval tasks")
        return eval_dataset.tasks
    except Exception as e:
        log.warning(f"Failed to load eval tasks: {e}")
        return []


if __name__ == "__main__":
    main()
