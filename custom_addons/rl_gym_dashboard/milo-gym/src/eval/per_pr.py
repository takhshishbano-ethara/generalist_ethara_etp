"""Per-PR evaluation with pass@1 and pass@N metrics."""
from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict

from src.core.schemas import EvalResult, TaskSpec, Trajectory
from src.rollout.docker_executor import DockerExecutor
from src.rollout.patch_utils import extract_patch

log = logging.getLogger(__name__)


class PerPREvaluator:
    def __init__(
        self,
        executor: DockerExecutor,
        n_attempts: int = 1,
        temperature: float = 1.0,
    ):
        self._executor = executor
        self._n_attempts = n_attempts
        self._temperature = temperature

    async def evaluate_task(
        self, task: TaskSpec, trajectories: list[Trajectory]
    ) -> EvalResult:
        n = len(trajectories)
        if n == 0:
            return EvalResult(
                task_id=task.task_id,
                passed=False,
                trajectories_attempted=0,
                pass_at_1=0.0,
                pass_at_n=0.0,
            )

        successes = 0
        best_idx = -1

        for i, traj in enumerate(trajectories):
            patch = traj.patch or extract_patch(
                "\n".join(t.content for t in traj.turns if t.role == "assistant")
            )
            if not patch.strip():
                continue

            result = await self._executor.run_evaluation(task, patch)
            if result.success:
                successes += 1
                if best_idx == -1:
                    best_idx = i

        pass_1 = self.compute_pass_at_k(n, successes, 1)
        pass_n = self.compute_pass_at_k(n, successes, n)

        return EvalResult(
            task_id=task.task_id,
            passed=successes > 0,
            trajectories_attempted=n,
            best_trajectory_idx=max(0, best_idx),
            pass_at_1=pass_1,
            pass_at_n=pass_n,
            details={
                "n": n,
                "c": successes,
                "difficulty": task.difficulty,
                "language": task.language,
            },
        )

    async def evaluate_batch(
        self,
        tasks: list[TaskSpec],
        trajectories_per_task: dict[str, list[Trajectory]],
        max_concurrent: int = 16,
    ) -> list[EvalResult]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _eval_one(task: TaskSpec) -> EvalResult:
            async with semaphore:
                trajs = trajectories_per_task.get(task.task_id, [])
                return await self.evaluate_task(task, trajs)

        results = await asyncio.gather(*[_eval_one(t) for t in tasks])
        return list(results)

    def compute_pass_at_k(self, n: int, c: int, k: int) -> float:
        """Unbiased pass@k estimator: 1 - C(n-c, k) / C(n, k)."""
        if n == 0:
            return 0.0
        if c >= n:
            return 1.0
        k = min(k, n)
        if n - c < k:
            return 1.0
        try:
            numerator = math.comb(n - c, k)
            denominator = math.comb(n, k)
            if denominator == 0:
                return 0.0
            return 1.0 - numerator / denominator
        except (ValueError, OverflowError):
            return 1.0 if c > 0 else 0.0

    def aggregate_results(self, results: list[EvalResult]) -> dict[str, float]:
        if not results:
            return {"mean_pass_at_1": 0.0, "mean_pass_at_n": 0.0}

        total_p1 = sum(r.pass_at_1 for r in results)
        total_pn = sum(r.pass_at_n for r in results)
        count = len(results)

        aggregated: dict[str, float] = {
            "mean_pass_at_1": total_p1 / count,
            "mean_pass_at_n": total_pn / count,
            "total_tasks": float(count),
            "total_passed": float(sum(1 for r in results if r.passed)),
        }

        by_difficulty: dict[str, list[EvalResult]] = defaultdict(list)
        for r in results:
            diff = r.details.get("difficulty", "unknown")
            by_difficulty[diff].append(r)

        for diff, group in by_difficulty.items():
            g_count = len(group)
            aggregated[f"pass_at_1_{diff}"] = (
                sum(r.pass_at_1 for r in group) / g_count
            )
            aggregated[f"pass_at_n_{diff}"] = (
                sum(r.pass_at_n for r in group) / g_count
            )
            aggregated[f"count_{diff}"] = float(g_count)

        by_language: dict[str, list[EvalResult]] = defaultdict(list)
        for r in results:
            lang = r.details.get("language", "unknown")
            by_language[lang].append(r)

        for lang, group in by_language.items():
            g_count = len(group)
            aggregated[f"pass_at_1_{lang}"] = (
                sum(r.pass_at_1 for r in group) / g_count
            )

        return aggregated
