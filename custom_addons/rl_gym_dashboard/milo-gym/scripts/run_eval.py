"""Evaluation: per-PR pass@1, pass@N, and Best-of-N."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import load_milo_config
from src.core.schemas import Trajectory, Turn
from src.data.dataset import MiloDataset
from src.eval.per_pr import PerPREvaluator
from src.eval.best_of_n import BestOfNEvaluator
from src.rollout.docker_executor import DockerExecutor

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate MILO-RL Model")
    parser.add_argument("--config", type=str, default="configs/eval.yaml")
    parser.add_argument("--data-dir", type=str, default="data/tasks/")
    parser.add_argument("--mode", choices=["per_pr", "best_of_n", "both"], default="both")
    parser.add_argument("--output-dir", type=str, default="outputs/eval/")
    parser.add_argument("--checkpoint", type=str, required=True, help="Model checkpoint")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    config = load_milo_config(args.config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    eval_dataset = MiloDataset.from_jsonl(Path(args.data_dir) / "eval.jsonl")
    log.info(f"Evaluating on {len(eval_dataset)} tasks with checkpoint: {args.checkpoint}")

    executor = DockerExecutor(max_concurrent=16, timeout=config.hardware.docker_timeout)

    trajectories_per_task: dict[str, list[Trajectory]] = {}
    for task in eval_dataset.tasks:
        trajectories_per_task[task.task_id] = [
            Trajectory(task_id=task.task_id, turns=[], episode_length=0)
        ]

    results = {}

    if args.mode in ("per_pr", "both"):
        log.info("Running per-PR evaluation...")
        evaluator = PerPREvaluator(executor, n_attempts=1)
        per_pr_results = asyncio.run(
            evaluator.evaluate_batch(eval_dataset.tasks, trajectories_per_task)
        )
        agg = evaluator.aggregate_results(per_pr_results)
        results["per_pr"] = agg
        log.info(f"  Per-PR pass@1: {agg.get('mean_pass_at_1', 0):.4f}")

    if args.mode in ("best_of_n", "both"):
        log.info("Running Best-of-N evaluation...")
        bon_evaluator = BestOfNEvaluator(executor, n=config.eval.best_of_n)
        bon_results = asyncio.run(
            bon_evaluator.evaluate_batch(eval_dataset.tasks, trajectories_per_task)
        )
        bon_agg = bon_evaluator._per_pr.aggregate_results(bon_results)
        results["best_of_n"] = bon_agg
        log.info(
            f"  Best-of-{config.eval.best_of_n} pass@1: "
            f"{bon_agg.get('mean_pass_at_1', 0):.4f}"
        )

    results_path = output_dir / "eval_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
