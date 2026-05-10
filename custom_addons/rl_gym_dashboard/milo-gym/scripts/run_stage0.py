"""Stage 0: Data pipeline — decompose, augment, validate, score, split."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import MiloConfig, load_milo_config
from src.data.decomposer import MILODecomposer
from src.data.augmentation import (
    CommitReversionAugmenter,
    ASTMutationAugmenter,
    LLMBugInjector,
    run_augmentation_pipeline,
)
from src.data.validator import TaskValidator
from src.data.difficulty import DifficultyScorer
from src.data.dataset import MiloDataset
from src.rollout.docker_executor import DockerExecutor

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 0: Data Pipeline")
    parser.add_argument("--config", type=str, default="configs/stage0.yaml")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--skip-difficulty", action="store_true")
    parser.add_argument("--output-dir", type=str, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    config = load_milo_config(args.config)
    output_dir = Path(args.output_dir or config.stage0.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Step 1: Decomposing MILO instances...")
    start = time.time()
    decomposer = MILODecomposer(config.stage0.milo_data_dir, languages=config.stage0.languages)
    tasks = decomposer.decompose_all()
    log.info(f"  Decomposed into {len(tasks)} raw tasks ({time.time()-start:.1f}s)")

    log.info("Step 2: Running augmentation pipeline...")
    start = time.time()
    strategies = []
    if "commit_reversion" in config.stage0.augmentation_strategies:
        strategies.append(CommitReversionAugmenter())
    if "ast_mutation" in config.stage0.augmentation_strategies:
        strategies.append(ASTMutationAugmenter())
    if (
        "llm_bug_injection" in config.stage0.augmentation_strategies
        and config.stage0.llm_endpoint
    ):
        strategies.append(LLMBugInjector(llm_endpoint=config.stage0.llm_endpoint))
    augmented = run_augmentation_pipeline(tasks, strategies)
    all_tasks = tasks + augmented
    log.info(f"  Total tasks after augmentation: {len(all_tasks)} ({time.time()-start:.1f}s)")

    if not args.skip_validation:
        log.info("Step 3: Validating tasks via Docker...")
        start = time.time()
        executor = DockerExecutor(timeout=config.stage0.validation_timeout)
        validator = TaskValidator(executor)
        results = asyncio.run(validator.validate_batch(all_tasks))
        all_tasks = validator.filter_valid(results)
        log.info(f"  Valid tasks: {len(all_tasks)} ({time.time()-start:.1f}s)")

    if not args.skip_difficulty:
        log.info("Step 4: Scoring difficulty...")
        start = time.time()
        executor = DockerExecutor()
        scorer = DifficultyScorer(model_path=config.model_path, executor=executor)
        all_tasks = scorer.assign_difficulties(all_tasks)
        log.info(f"  Scored {len(all_tasks)} tasks ({time.time()-start:.1f}s)")

    log.info("Step 5: Splitting and saving...")
    dataset = MiloDataset(all_tasks)
    train_ds, eval_ds = dataset.split_train_eval(eval_size=config.stage0.eval_split_size)
    train_ds.to_jsonl(output_dir / "train.jsonl")
    train_ds.to_verl_parquet(output_dir / "train.parquet")
    eval_ds.to_jsonl(output_dir / "eval.jsonl")
    eval_ds.to_verl_parquet(output_dir / "eval.parquet")

    log.info(f"Done! Train: {len(train_ds)}, Eval: {len(eval_ds)}")
    log.info(f"  Difficulty: {train_ds.difficulty_distribution()}")
    log.info(f"  Language: {train_ds.language_distribution()}")


if __name__ == "__main__":
    main()
