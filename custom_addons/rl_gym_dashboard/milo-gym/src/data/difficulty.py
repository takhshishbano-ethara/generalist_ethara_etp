"""Difficulty scoring via pass@1 estimation from base model."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from src.core.schemas import TaskSpec
from src.rollout.docker_executor import DockerExecutor
from src.training.reward_manager import _run_async

log = logging.getLogger(__name__)


def _count_patch_lines(patch: str) -> int:
    return sum(
        1 for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def _count_patch_files(patch: str) -> int:
    return patch.count("--- a/")


class DifficultyScorer:
    def __init__(
        self,
        model_path: str,
        executor: DockerExecutor,
        n_attempts: int = 16,
        temperature: float = 0.7,
        tp_size: int = 2,
    ):
        self._model_path = model_path
        self._executor = executor
        self._n_attempts = n_attempts
        self._temperature = temperature
        self._tp_size = tp_size
        self._vllm_url: str | None = None

    async def score_task(self, task: TaskSpec) -> float:
        """Estimate pass@1 from task characteristics (heuristic fallback)."""
        if self._vllm_url is not None:
            return await self._score_via_vllm(task)
        return self._heuristic_score(task)

    def _heuristic_score(self, task: TaskSpec) -> float:
        patch_lines = _count_patch_lines(task.fix_patch)
        file_count = _count_patch_files(task.fix_patch)

        if patch_lines < 20 and file_count <= 1:
            return 0.4
        elif patch_lines <= 100 and file_count <= 2:
            return 0.2
        else:
            return 0.05

    async def _score_via_vllm(self, task: TaskSpec) -> float:
        """Estimate pass@1 by generating n_attempts and grading them."""
        try:
            import aiohttp

            payload = {
                "model": self._model_path,
                "prompt": task.problem_statement,
                "n": self._n_attempts,
                "temperature": self._temperature,
                "max_tokens": 4096,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._vllm_url}/v1/completions",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        log.warning("vLLM returned %d for difficulty scoring", resp.status)
                        return self._heuristic_score(task)
                    data = await resp.json()

            completions = [c["text"] for c in data.get("choices", [])]
            if not completions:
                return self._heuristic_score(task)

            from src.rollout.patch_utils import extract_patch

            patches = [extract_patch(c) for c in completions]
            valid_patches = [(task, p) for p in patches if p.strip()]

            if not valid_patches:
                return 0.0

            results = await self._executor.run_batch(valid_patches)
            pass_count = sum(1 for r in results if r.success)
            return pass_count / self._n_attempts

        except Exception as e:
            log.warning("vLLM difficulty scoring failed: %s, using heuristic", e)
            return self._heuristic_score(task)

    async def score_batch(
        self, tasks: list[TaskSpec], max_concurrent: int = 8
    ) -> list[tuple[TaskSpec, float]]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _score_one(t: TaskSpec) -> tuple[TaskSpec, float]:
            async with semaphore:
                score = await self.score_task(t)
                return (t, score)

        results = await asyncio.gather(*[_score_one(t) for t in tasks])
        return list(results)

    def classify(self, pass_at_1: float) -> Literal["easy", "medium", "hard"]:
        if pass_at_1 > 0.3:
            return "easy"
        elif pass_at_1 > 0.1:
            return "medium"
        return "hard"

    def assign_difficulties(self, tasks: list[TaskSpec]) -> list[TaskSpec]:
        scored = _run_async(self.score_batch(tasks))

        updated: list[TaskSpec] = []
        for task, score in scored:
            updated.append(
                task.model_copy(
                    update={
                        "difficulty": self.classify(score),
                        "difficulty_score": score,
                    }
                )
            )
        return updated

    def set_vllm_url(self, url: str) -> None:
        self._vllm_url = url

    def distribution_summary(
        self, tasks: list[TaskSpec]
    ) -> dict[str, int]:
        counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
        for t in tasks:
            counts[t.difficulty] = counts.get(t.difficulty, 0) + 1
        return counts
