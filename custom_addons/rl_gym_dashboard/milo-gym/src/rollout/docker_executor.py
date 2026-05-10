"""Standalone Docker execution engine for training rollouts and data validation."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field

import docker
from docker.models.containers import Container

from src.core.config import ECRConfig
from src.core.schemas import TaskSpec
from src.rollout.ecr import ECRAuthManager, ECRImageManager, resolve_image_uri

log = logging.getLogger(__name__)

_F2P_RE = re.compile(r"F2P:\s*(\d+)/(\d+)")
_P2P_RE = re.compile(r"P2P:\s*(\d+)/(\d+)")


@dataclass
class DockerResult:
    f2p_passed: int = 0
    f2p_total: int = 0
    p2p_passed: int = 0
    p2p_total: int = 0
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    wall_clock_seconds: float = 0.0
    error: str | None = None

    @property
    def f2p_pass(self) -> bool:
        return self.f2p_total > 0 and self.f2p_passed == self.f2p_total

    @property
    def p2p_pass(self) -> bool:
        return self.p2p_total == 0 or self.p2p_passed == self.p2p_total

    @property
    def success(self) -> bool:
        return self.f2p_pass and self.p2p_pass


class DockerExecutor:
    def __init__(
        self,
        max_concurrent: int = 64,
        timeout: int = 1800,
        cpu_limit: float = 1.0,
        mem_limit: str = "4g",
        network_disabled: bool = True,
        ecr_config: ECRConfig | None = None,
    ):
        self._client = docker.from_env()
        self._semaphore: asyncio.Semaphore | None = None
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._cpu_limit = cpu_limit
        self._mem_limit = mem_limit
        self._network_disabled = network_disabled
        self._pids_limit = 256
        self._containers: dict[str, Container] = {}
        self._ecr_config = ecr_config
        self._ecr_auth: ECRAuthManager | None = None
        self._ecr_images: ECRImageManager | None = None
        if ecr_config and ecr_config.enabled:
            self._ecr_auth = ECRAuthManager(ecr_config)
            self._ecr_images = ECRImageManager(self._ecr_auth, self._client)

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        return self._semaphore

    def _resolve_image(self, task: TaskSpec) -> str:
        if task.docker_image:
            return task.docker_image
        if self._ecr_config and self._ecr_config.enabled and task.instance_id:
            return resolve_image_uri(task.instance_id, self._ecr_config)
        raise ValueError(f"Task {task.task_id}: no docker_image and ECR disabled or no instance_id")

    def _resolve_eval_script(self, task: TaskSpec) -> str:
        if task.evaluation_script:
            return task.evaluation_script
        if self._ecr_config and self._ecr_config.enabled:
            return self._ecr_config.evaluation_command
        raise ValueError(f"Task {task.task_id}: no evaluation_script configured")

    def _resolve_patch_path(self) -> str:
        if self._ecr_config and self._ecr_config.enabled:
            return self._ecr_config.patch_path
        return "/tmp/patch.diff"

    async def create_container(self, task: TaskSpec) -> str:
        image_uri = self._resolve_image(task)

        if self._ecr_images:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ecr_images.ensure_image, image_uri)

        loop = asyncio.get_running_loop()
        container = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                image_uri,
                command="sleep infinity",
                detach=True,
                nano_cpus=int(self._cpu_limit * 1e9),
                mem_limit=self._mem_limit,
                network_disabled=self._network_disabled,
                pids_limit=self._pids_limit,
                security_opt=["no-new-privileges"],
                labels={"milo-task": task.task_id},
            ),
        )
        cid: str = container.id or container.short_id or ""
        self._containers[cid] = container
        return cid

    async def remove_container(self, container_id: str) -> None:
        container = self._containers.pop(container_id, None)
        if container is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: container.remove(force=True)
            )
        except Exception as e:
            log.warning("Failed to remove container %s: %s", container_id[:12], e)

    async def exec_in_container(
        self, container_id: str, command: str, timeout: int | None = None
    ) -> tuple[int, str, str]:
        container = self._containers.get(container_id)
        if container is None:
            return -1, "", f"Container {container_id[:12]} not found"

        effective_timeout = timeout or self._timeout
        loop = asyncio.get_running_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: container.exec_run(
                        ["bash", "-c", command], demux=True
                    ),
                ),
                timeout=effective_timeout,
            )
            stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
            stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
            return result.exit_code, stdout, stderr
        except asyncio.TimeoutError:
            return -1, "", "Command timed out"

    async def apply_patch(self, container_id: str, patch: str) -> bool:
        patch_path = self._resolve_patch_path()
        encoded = base64.b64encode(patch.encode()).decode()
        write_cmd = f"echo '{encoded}' | base64 -d > {patch_path}"
        exit_code, _, stderr = await self.exec_in_container(container_id, write_cmd)
        if exit_code != 0:
            log.error("Failed to write patch to %s: %s", patch_path, stderr)
            return False

        if self._ecr_config and self._ecr_config.enabled:
            return True

        exit_code, _, stderr = await self.exec_in_container(
            container_id, f"cd /repo && git apply {patch_path}"
        )
        if exit_code != 0:
            log.error("Failed to apply patch: %s", stderr)
            return False
        return True

    async def run_evaluation(self, task: TaskSpec, patch: str) -> DockerResult:
        start = time.monotonic()
        container_id: str | None = None
        try:
            container_id = await self.create_container(task)

            if not await self.apply_patch(container_id, patch):
                return DockerResult(
                    error="Patch application failed",
                    wall_clock_seconds=time.monotonic() - start,
                )

            eval_script = self._resolve_eval_script(task)
            exit_code, stdout, stderr = await self.exec_in_container(
                container_id, eval_script, timeout=task.timeout_seconds
            )

            timed_out = stderr == "Command timed out"
            f2p_passed, f2p_total, p2p_passed, p2p_total = self._parse_eval_output(
                stdout, stderr
            )

            return DockerResult(
                f2p_passed=f2p_passed,
                f2p_total=f2p_total,
                p2p_passed=p2p_passed,
                p2p_total=p2p_total,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                wall_clock_seconds=time.monotonic() - start,
            )
        except Exception as e:
            log.exception("Evaluation failed for task %s", task.task_id)
            return DockerResult(
                error=str(e),
                wall_clock_seconds=time.monotonic() - start,
            )
        finally:
            if container_id:
                await self.remove_container(container_id)

    async def run_batch(
        self, items: list[tuple[TaskSpec, str]]
    ) -> list[DockerResult]:
        async def _run_one(task: TaskSpec, patch: str) -> DockerResult:
            async with self._get_semaphore():
                return await self.run_evaluation(task, patch)

        return await asyncio.gather(
            *[_run_one(task, patch) for task, patch in items]
        )

    def _parse_eval_output(
        self, stdout: str, stderr: str
    ) -> tuple[int, int, int, int]:
        combined = stdout + "\n" + stderr
        f2p_passed, f2p_total = 0, 0
        p2p_passed, p2p_total = 0, 0

        f2p_match = _F2P_RE.search(combined)
        if f2p_match:
            f2p_passed = int(f2p_match.group(1))
            f2p_total = int(f2p_match.group(2))

        p2p_match = _P2P_RE.search(combined)
        if p2p_match:
            p2p_passed = int(p2p_match.group(1))
            p2p_total = int(p2p_match.group(2))

        return f2p_passed, f2p_total, p2p_passed, p2p_total

    def health_check(self) -> bool:
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    def cleanup_all(self) -> None:
        for container_id in list(self._containers):
            container = self._containers.pop(container_id, None)
            if container:
                try:
                    container.remove(force=True)
                except Exception as e:
                    log.warning(
                        "Cleanup failed for %s: %s", container_id[:12], e
                    )
