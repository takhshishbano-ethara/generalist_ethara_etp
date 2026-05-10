"""verl-compatible tool for multi-turn Docker interaction during training rollouts."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field

from src.core.schemas import RewardResult, TaskSpec
from src.rollout.docker_executor import DockerExecutor, DockerResult

log = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 2000
_MAX_FILE_LINES = 200
_MAX_LIST_ENTRIES = 100
_MAX_GREP_MATCHES = 50


@dataclass
class ToolResponse:
    text: str
    is_error: bool = False


class DockerSandboxTool:
    """verl-compatible tool for multi-turn coding interaction in Docker containers."""

    def __init__(
        self,
        executor: DockerExecutor,
        task_registry: dict[str, TaskSpec],
        timeout_per_action: int = 60,
    ):
        self._executor = executor
        self._task_registry = task_registry
        self._timeout = timeout_per_action
        self._active_containers: dict[str, str] = {}
        self._episode_patches: dict[str, list[str]] = {}
        self._episode_done: set[str] = set()

    def get_tool_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "apply_patch",
                    "description": "Apply a unified diff patch to the repository.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["apply_patch"]},
                            "patch": {
                                "type": "string",
                                "description": "Unified diff content to apply.",
                            },
                        },
                        "required": ["action", "patch"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "description": "Run the test suite, optionally filtered.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["run_tests"]},
                            "test_filter": {
                                "type": "string",
                                "description": "Optional test filter expression.",
                                "default": "",
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read file contents with optional line range.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["read_file"]},
                            "file_path": {"type": "string"},
                            "start_line": {"type": "integer", "default": 0},
                            "end_line": {"type": "integer", "default": 0},
                        },
                        "required": ["action", "file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in a directory (max depth 2).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["list_files"]},
                            "directory": {"type": "string", "default": "."},
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Grep for a pattern in the repository.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["search"]},
                            "pattern": {"type": "string"},
                            "path": {"type": "string", "default": "."},
                        },
                        "required": ["action", "pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit",
                    "description": "Submit final patch and end the episode.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["submit"]},
                            "patch": {
                                "type": "string",
                                "description": "Final unified diff to submit.",
                            },
                        },
                        "required": ["action", "patch"],
                    },
                },
            },
        ]

    def get_tool_stop_strings(self) -> list[str]:
        return ["</tool_call>"]

    async def execute(
        self, instance_id: str, parameters: dict, **kwargs: object
    ) -> tuple[ToolResponse, float, dict]:
        action = parameters.get("action", "")
        metadata: dict = {"action": action, "instance_id": instance_id}

        if instance_id in self._episode_done:
            return (
                ToolResponse("Episode already completed.", is_error=True),
                0.0,
                metadata,
            )

        try:
            handler = self._get_handler(action)
            if handler is None:
                resp = ToolResponse(f"Unknown action: {action}", is_error=True)
            else:
                resp = await handler(instance_id, parameters)
        except asyncio.TimeoutError:
            resp = ToolResponse("Action timed out.", is_error=True)
        except Exception as e:
            log.exception("Tool execution error for %s/%s", instance_id, action)
            resp = ToolResponse(f"Internal error: {e}", is_error=True)

        metadata["is_error"] = resp.is_error
        return resp, 0.0, metadata

    def _get_handler(self, action: str):
        handlers = {
            "apply_patch": self._dispatch_apply_patch,
            "run_tests": self._dispatch_run_tests,
            "read_file": self._dispatch_read_file,
            "list_files": self._dispatch_list_files,
            "search": self._dispatch_search,
            "submit": self._dispatch_submit,
        }
        return handlers.get(action)

    async def _dispatch_apply_patch(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_apply_patch(instance_id, params.get("patch", ""))

    async def _dispatch_run_tests(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_run_tests(
            instance_id, params.get("test_filter", "")
        )

    async def _dispatch_read_file(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_read_file(
            instance_id,
            params.get("file_path", ""),
            params.get("start_line", 0),
            params.get("end_line", 0),
        )

    async def _dispatch_list_files(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_list_files(
            instance_id, params.get("directory", ".")
        )

    async def _dispatch_search(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_search(
            instance_id, params.get("pattern", ""), params.get("path", ".")
        )

    async def _dispatch_submit(
        self, instance_id: str, params: dict
    ) -> ToolResponse:
        return await self._handle_submit(instance_id, params.get("patch", ""))

    async def _handle_apply_patch(
        self, instance_id: str, patch: str
    ) -> ToolResponse:
        if not patch.strip():
            return ToolResponse("Empty patch provided.", is_error=True)

        container_id = await self._get_or_create_container(instance_id)
        if not container_id:
            return ToolResponse("Failed to create container.", is_error=True)

        import base64

        encoded = base64.b64encode(patch.encode()).decode()
        write_cmd = f"echo '{encoded}' | base64 -d > /tmp/patch.diff"
        exit_code, _, stderr = await self._executor.exec_in_container(
            container_id, write_cmd, timeout=self._timeout
        )
        if exit_code != 0:
            return ToolResponse(f"Failed to write patch: {stderr}", is_error=True)

        exit_code, stdout, stderr = await self._executor.exec_in_container(
            container_id,
            "cd /repo && git apply /tmp/patch.diff",
            timeout=self._timeout,
        )
        if exit_code != 0:
            output = (stderr or stdout)[:_MAX_OUTPUT_CHARS]
            return ToolResponse(f"Patch failed:\n{output}", is_error=True)

        self._episode_patches.setdefault(instance_id, []).append(patch)
        return ToolResponse("Patch applied successfully.")

    async def _handle_run_tests(
        self, instance_id: str, test_filter: str = ""
    ) -> ToolResponse:
        container_id = await self._get_or_create_container(instance_id)
        if not container_id:
            return ToolResponse("Failed to create container.", is_error=True)

        task = self._task_registry.get(instance_id)
        if task and task.language == "go":
            cmd = "cd /repo && go test ./..."
            if test_filter:
                cmd = f"cd /repo && go test -run {shlex.quote(test_filter)} ./..."
        else:
            cmd = "cd /repo && python -m pytest --tb=short -q"
            if test_filter:
                cmd = f"cd /repo && python -m pytest --tb=short -q -k {shlex.quote(test_filter)}"

        exit_code, stdout, stderr = await self._executor.exec_in_container(
            container_id, cmd, timeout=self._timeout
        )
        combined = (stdout + "\n" + stderr).strip()
        if len(combined) > _MAX_OUTPUT_CHARS:
            combined = combined[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"

        prefix = "Tests passed." if exit_code == 0 else "Tests failed."
        return ToolResponse(f"{prefix}\n{combined}", is_error=exit_code != 0)

    async def _handle_read_file(
        self, instance_id: str, file_path: str, start_line: int = 0, end_line: int = 0
    ) -> ToolResponse:
        if not file_path:
            return ToolResponse("file_path is required.", is_error=True)

        container_id = await self._get_or_create_container(instance_id)
        if not container_id:
            return ToolResponse("Failed to create container.", is_error=True)

        if start_line > 0 and end_line > 0:
            cmd = f"cd /repo && sed -n '{start_line},{end_line}p' {shlex.quote(file_path)}"
        elif start_line > 0:
            limit = start_line + _MAX_FILE_LINES - 1
            cmd = f"cd /repo && sed -n '{start_line},{limit}p' {shlex.quote(file_path)}"
        else:
            cmd = f"cd /repo && head -n {_MAX_FILE_LINES} {shlex.quote(file_path)}"

        exit_code, stdout, stderr = await self._executor.exec_in_container(
            container_id, cmd, timeout=self._timeout
        )
        if exit_code != 0:
            return ToolResponse(f"Error reading file: {stderr}", is_error=True)

        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return ToolResponse(stdout if stdout else "(empty file)")

    async def _handle_list_files(
        self, instance_id: str, directory: str = "."
    ) -> ToolResponse:
        container_id = await self._get_or_create_container(instance_id)
        if not container_id:
            return ToolResponse("Failed to create container.", is_error=True)

        cmd = (
            f"cd /repo && find {shlex.quote(directory)} -maxdepth 2 -type f"
            f" | head -n {_MAX_LIST_ENTRIES}"
        )
        exit_code, stdout, stderr = await self._executor.exec_in_container(
            container_id, cmd, timeout=self._timeout
        )
        if exit_code != 0:
            return ToolResponse(f"Error listing files: {stderr}", is_error=True)

        return ToolResponse(stdout.strip() if stdout.strip() else "(no files found)")

    async def _handle_search(
        self, instance_id: str, pattern: str, path: str = "."
    ) -> ToolResponse:
        if not pattern:
            return ToolResponse("pattern is required.", is_error=True)

        container_id = await self._get_or_create_container(instance_id)
        if not container_id:
            return ToolResponse("Failed to create container.", is_error=True)

        cmd = (
            f"cd /repo && grep -rn {shlex.quote(pattern)} {shlex.quote(path)}"
            f" | head -n {_MAX_GREP_MATCHES}"
        )
        exit_code, stdout, stderr = await self._executor.exec_in_container(
            container_id, cmd, timeout=self._timeout
        )
        if exit_code == 1 and not stdout:
            return ToolResponse("No matches found.")
        if exit_code not in (0, 1):
            return ToolResponse(f"Search error: {stderr}", is_error=True)

        return ToolResponse(stdout.strip() if stdout.strip() else "No matches found.")

    async def _handle_submit(self, instance_id: str, patch: str) -> ToolResponse:
        if not patch.strip():
            patches = self._episode_patches.get(instance_id, [])
            if not patches:
                return ToolResponse("No patch to submit.", is_error=True)
            patch = "\n".join(patches)

        self._episode_patches[instance_id] = [patch]
        self._episode_done.add(instance_id)
        return ToolResponse("Patch submitted. Episode complete.")

    async def _get_or_create_container(self, instance_id: str) -> str:
        if instance_id in self._active_containers:
            return self._active_containers[instance_id]

        task = self._task_registry.get(instance_id)
        if task is None:
            log.error("No task spec found for instance_id=%s", instance_id)
            return ""

        try:
            container_id = await self._executor.create_container(task)
            self._active_containers[instance_id] = container_id
            log.info(
                "Created container %s for %s", container_id[:12], instance_id
            )
            return container_id
        except Exception as e:
            log.exception("Container creation failed for %s", instance_id)
            return ""

    async def finalize_episode(self, instance_id: str) -> RewardResult:
        patches = self._episode_patches.get(instance_id, [])
        patch = "\n".join(patches) if patches else ""
        if not patch.strip():
            await self.cleanup_episode(instance_id)
            return RewardResult(reward=0.0, mask=False, error="No patch submitted")

        task = self._task_registry.get(instance_id)
        if task is None:
            await self.cleanup_episode(instance_id)
            return RewardResult(reward=0.0, mask=False, error="Task not found")

        try:
            result: DockerResult = await self._executor.run_evaluation(task, patch)
            reward = 1.0 if (result.f2p_pass and result.p2p_pass) else 0.0
            return RewardResult(
                reward=reward,
                mask=True,
                f2p_passed=result.f2p_passed,
                f2p_total=result.f2p_total,
                p2p_passed=result.p2p_passed,
                p2p_total=result.p2p_total,
                timed_out=result.timed_out,
                error=result.error,
            )
        except Exception as e:
            log.exception("Finalization failed for %s", instance_id)
            return RewardResult(reward=0.0, mask=False, error=str(e))
        finally:
            await self.cleanup_episode(instance_id)

    async def cleanup_episode(self, instance_id: str) -> None:
        container_id = self._active_containers.pop(instance_id, None)
        if container_id:
            await self._executor.remove_container(container_id)
        self._episode_patches.pop(instance_id, None)
        self._episode_done.discard(instance_id)
