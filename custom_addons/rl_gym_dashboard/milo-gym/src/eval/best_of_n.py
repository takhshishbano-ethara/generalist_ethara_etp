"""Best-of-N evaluation with reranking heuristics."""
from __future__ import annotations

import asyncio
import logging

from src.core.schemas import EvalResult, TaskSpec, Trajectory
from src.eval.per_pr import PerPREvaluator
from src.rollout.docker_executor import DockerExecutor
from src.rollout.patch_utils import extract_patch, patch_stats

log = logging.getLogger(__name__)


class BestOfNEvaluator:
    def __init__(
        self,
        executor: DockerExecutor,
        n: int = 8,
        temperature: float = 1.0,
    ):
        self._executor = executor
        self._n = n
        self._temperature = temperature
        self._per_pr = PerPREvaluator(executor, n_attempts=n, temperature=temperature)

    async def evaluate_task(
        self, task: TaskSpec, trajectories: list[Trajectory]
    ) -> EvalResult:
        if not trajectories:
            return EvalResult(
                task_id=task.task_id,
                passed=False,
                trajectories_attempted=0,
                pass_at_1=0.0,
                pass_at_n=0.0,
            )

        ranked = self.rank_trajectories(trajectories)
        best_idx = 0

        for i, traj in enumerate(ranked):
            patch = traj.patch or extract_patch(
                "\n".join(t.content for t in traj.turns if t.role == "assistant")
            )
            if not patch.strip():
                continue

            result = await self._executor.run_evaluation(task, patch)
            if result.success:
                original_idx = trajectories.index(traj)
                return EvalResult(
                    task_id=task.task_id,
                    passed=True,
                    trajectories_attempted=i + 1,
                    best_trajectory_idx=original_idx,
                    pass_at_1=self._per_pr.compute_pass_at_k(
                        len(trajectories), 1, 1
                    ),
                    pass_at_n=1.0,
                    details={
                        "method": "best_of_n",
                        "rank_position": i,
                        "difficulty": task.difficulty,
                        "language": task.language,
                    },
                )

        n = len(ranked)
        return EvalResult(
            task_id=task.task_id,
            passed=False,
            trajectories_attempted=n,
            best_trajectory_idx=0,
            pass_at_1=self._per_pr.compute_pass_at_k(n, 0, 1),
            pass_at_n=0.0,
            details={
                "method": "best_of_n",
                "difficulty": task.difficulty,
                "language": task.language,
            },
        )

    async def evaluate_batch(
        self,
        tasks: list[TaskSpec],
        trajectories_per_task: dict[str, list[Trajectory]],
        max_concurrent: int = 8,
    ) -> list[EvalResult]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _eval_one(task: TaskSpec) -> EvalResult:
            async with semaphore:
                trajs = trajectories_per_task.get(task.task_id, [])
                return await self.evaluate_task(task, trajs)

        results = await asyncio.gather(*[_eval_one(t) for t in tasks])
        return list(results)

    def rank_trajectories(self, trajectories: list[Trajectory]) -> list[Trajectory]:
        return sorted(trajectories, key=self._heuristic_score, reverse=True)

    def _heuristic_score(self, trajectory: Trajectory) -> float:
        score = 0.0

        patch = trajectory.patch or extract_patch(
            "\n".join(t.content for t in trajectory.turns if t.role == "assistant")
        )

        if patch.strip():
            score += 10.0
            stats = patch_stats(patch)
            total_lines = stats["lines_added"] + stats["lines_removed"]
            if 5 <= total_lines <= 100:
                score += 5.0
            elif total_lines > 100:
                score += 2.0
        else:
            return -100.0

        score -= 0.1 * trajectory.episode_length

        if not trajectory.timed_out:
            score += 3.0

        if not trajectory.hit_max_turns:
            score += 2.0

        if not trajectory.hit_max_context:
            score += 1.0

        tool_turns = sum(1 for t in trajectory.turns if t.role == "tool")
        if tool_turns > 0:
            score += min(tool_turns * 0.5, 3.0)

        return score
