"""Docker-based task validation: checks gold patch passes tests."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src.core.schemas import TaskSpec
from src.rollout.docker_executor import DockerExecutor, DockerResult

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    task: TaskSpec
    valid: bool
    docker_result: DockerResult | None = None
    error: str | None = None


class TaskValidator:
    def __init__(self, executor: DockerExecutor):
        self._executor = executor

    async def validate_task(self, task: TaskSpec) -> ValidationResult:
        """Apply gold fix_patch, run evaluation. Valid if F2P>0 AND P2P passes."""
        try:
            result = await self._executor.run_evaluation(task, task.fix_patch)
            return ValidationResult(
                task=task,
                valid=result.success,
                docker_result=result,
            )
        except Exception as e:
            log.warning("Validation failed for %s: %s", task.task_id, e)
            return ValidationResult(task=task, valid=False, error=str(e))

    async def validate_batch(
        self, tasks: list[TaskSpec], max_concurrent: int = 16
    ) -> list[ValidationResult]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _validate_one(t: TaskSpec) -> ValidationResult:
            async with semaphore:
                return await self.validate_task(t)

        results = await asyncio.gather(*[_validate_one(t) for t in tasks])
        return list(results)

    def filter_valid(self, results: list[ValidationResult]) -> list[TaskSpec]:
        return [r.task for r in results if r.valid]

    def filter_invalid(self, results: list[ValidationResult]) -> list[ValidationResult]:
        return [r for r in results if not r.valid]

    async def check_docker_builds(self, task: TaskSpec) -> bool:
        try:
            container_id = await self._executor.create_container(task)
            await self._executor.remove_container(container_id)
            return True
        except Exception as e:
            log.warning("Docker build check failed for %s: %s", task.task_id, e)
            return False

    async def check_batch_builds(
        self, tasks: list[TaskSpec], max_concurrent: int = 8
    ) -> list[tuple[TaskSpec, bool]]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _check_one(t: TaskSpec) -> tuple[TaskSpec, bool]:
            async with semaphore:
                ok = await self.check_docker_builds(t)
                return (t, ok)

        results = await asyncio.gather(*[_check_one(t) for t in tasks])
        return list(results)

    def summary(self, results: list[ValidationResult]) -> dict[str, int]:
        valid_count = sum(1 for r in results if r.valid)
        return {
            "total": len(results),
            "valid": valid_count,
            "invalid": len(results) - valid_count,
            "errors": sum(1 for r in results if r.error is not None),
        }

    async def validate_and_filter(
        self, tasks: list[TaskSpec], max_concurrent: int = 16
    ) -> list[TaskSpec]:
        results = await self.validate_batch(tasks, max_concurrent)
        stats = self.summary(results)
        log.info(
            "Validation complete: %d/%d valid (%d errors)",
            stats["valid"],
            stats["total"],
            stats["errors"],
        )
        return self.filter_valid(results)
