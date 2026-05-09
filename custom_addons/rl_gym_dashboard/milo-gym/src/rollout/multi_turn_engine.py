"""Multi-turn rollout engine for generating trajectories with tool-use.

Orchestrates: vLLM generation → tool call parsing → Docker execution → trajectory assembly.
Implements group-relative sampling (multiple trajectories per task) for GRPO/GTPO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from src.core.config import MiloConfig
from src.core.schemas import TaskSpec, Trajectory, Turn
from src.rollout.docker_tool import DockerSandboxTool, ToolResponse

log = logging.getLogger(__name__)

_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)

_LOOP_DETECTION_WINDOW = 3


@dataclass
class RolloutConfig:
    max_turns: int = 50
    max_context_tokens: int = 65536
    temperature: float = 1.0
    top_p: float = 1.0
    max_response_tokens: int = 4096
    docker_concurrency: int = 64


class MultiTurnRolloutEngine:
    """Generates multi-turn trajectories using vLLM + DockerSandboxTool."""

    def __init__(
        self,
        vllm_engine,
        docker_tool: DockerSandboxTool,
        tokenizer,
        config: MiloConfig,
    ):
        self._vllm = vllm_engine
        self._docker_tool = docker_tool
        self._tokenizer = tokenizer
        self._config = config
        self._rollout_cfg = RolloutConfig(
            max_turns=config.eval.max_turns,
            max_context_tokens=config.hardware.max_model_len // 2,
            temperature=config.gspo.temperature,
            top_p=config.gspo.top_p,
            docker_concurrency=config.hardware.docker_containers,
        )
        self._semaphore = asyncio.Semaphore(self._rollout_cfg.docker_concurrency)
        self._lora_request = None

    def set_lora_request(self, lora_request) -> None:
        self._lora_request = lora_request

    async def generate_trajectory(
        self,
        task: TaskSpec,
        temperature: float | None = None,
        max_turns: int | None = None,
    ) -> Trajectory:
        """Generate a single multi-turn trajectory for a task."""
        async with self._semaphore:
            return await self._run_episode(task, temperature, max_turns)

    async def generate_group(
        self,
        task: TaskSpec,
        group_size: int,
        temperature: float | None = None,
    ) -> list[Trajectory]:
        """Generate group_size trajectories for one task (for GRPO variance)."""
        coros = [
            self.generate_trajectory(task, temperature=temperature)
            for _ in range(group_size)
        ]
        return await asyncio.gather(*coros)

    async def generate_batch(
        self,
        tasks: list[TaskSpec],
        group_size: int,
        temperature: float | None = None,
    ) -> list[Trajectory]:
        """Generate batch_size trajectories: len(tasks) × group_size.

        Returns flat list of trajectories, ordered by task then group member.
        """
        all_coros = []
        for task in tasks:
            for _ in range(group_size):
                all_coros.append(self.generate_trajectory(task, temperature=temperature))

        results = await asyncio.gather(*all_coros, return_exceptions=True)

        trajectories = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                task_idx = i // group_size
                task = tasks[task_idx]
                log.error("Rollout failed for task %s: %s", task.task_id, result)
                trajectories.append(Trajectory(
                    task_id=task.task_id,
                    turns=[],
                    error=str(result),
                    mask=False,
                ))
            else:
                trajectories.append(result)

        return trajectories

    async def _run_episode(
        self,
        task: TaskSpec,
        temperature: float | None,
        max_turns: int | None,
    ) -> Trajectory:
        """Execute full multi-turn episode: generate → parse → execute → repeat."""
        temp = temperature or self._rollout_cfg.temperature
        max_t = max_turns or min(task.max_turns, self._rollout_cfg.max_turns)
        start_time = time.time()

        messages = self._build_initial_messages(task)
        turns: list[Turn] = []
        recent_actions: list[str] = []
        instance_id = f"{task.task_id}_{id(asyncio.current_task())}"
        done = False
        hit_max_turns = False
        hit_max_context = False

        try:
            for turn_idx in range(max_t):
                prompt_text = self._tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )

                token_count = len(self._tokenizer.encode(prompt_text))
                if token_count > self._rollout_cfg.max_context_tokens:
                    hit_max_context = True
                    break

                response_text = await self._generate_response(prompt_text, temp)

                assistant_turn = Turn(
                    role="assistant",
                    content=response_text,
                    timestamp=time.time(),
                    token_count=len(self._tokenizer.encode(response_text)),
                )
                turns.append(assistant_turn)
                messages.append({"role": "assistant", "content": response_text})

                tool_call = self._parse_tool_call(response_text)
                if tool_call is None:
                    done = True
                    break

                action = tool_call.get("action", "")
                recent_actions.append(json.dumps(tool_call, sort_keys=True))

                if self._detect_loop(recent_actions):
                    log.info("Loop detected for %s at turn %d", task.task_id, turn_idx)
                    done = True
                    break

                if action == "submit":
                    tool_resp, _, _ = await self._docker_tool.execute(
                        instance_id, tool_call
                    )
                    tool_turn = Turn(
                        role="tool",
                        content=tool_resp.text,
                        tool_call_id=action,
                        timestamp=time.time(),
                    )
                    turns.append(tool_turn)
                    messages.append({"role": "tool", "content": tool_resp.text})
                    done = True
                    break

                tool_resp, _, _ = await self._docker_tool.execute(
                    instance_id, tool_call
                )
                tool_turn = Turn(
                    role="tool",
                    content=tool_resp.text,
                    tool_call_id=action,
                    timestamp=time.time(),
                )
                turns.append(tool_turn)
                messages.append({"role": "tool", "content": tool_resp.text})

            if not done:
                hit_max_turns = True

            from src.rollout.patch_utils import extract_patch
            all_assistant_text = "\n".join(t.content for t in turns if t.role == "assistant")
            patch = extract_patch(all_assistant_text)

            elapsed = time.time() - start_time
            return Trajectory(
                task_id=task.task_id,
                turns=turns,
                raw_response=all_assistant_text,
                patch=patch,
                mask=True,
                hit_max_turns=hit_max_turns,
                hit_max_context=hit_max_context,
                episode_length=len([t for t in turns if t.role == "assistant"]),
                wall_clock_seconds=elapsed,
            )
        finally:
            await self._docker_tool.cleanup_episode(instance_id)

    def _build_initial_messages(self, task: TaskSpec) -> list[dict]:
        system_prompt = (
            "You are a coding agent. You can use tools to explore the repository, "
            "apply patches, run tests, and submit your final solution.\n\n"
            "Available tools: apply_patch, run_tests, read_file, list_files, search, submit.\n"
            "Wrap tool calls in <tool_call>{...}</tool_call> tags.\n"
            "When done, use the submit tool with your final patch."
        )
        user_prompt = f"## Task\n\n{task.problem_statement}\n\nRepository: {task.repo}\nLanguage: {task.language}"

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def _generate_response(self, prompt_text: str, temperature: float) -> str:
        """Generate response via vLLM (runs in executor since vLLM is sync)."""
        from vllm import SamplingParams

        sampling_params = SamplingParams(
            temperature=temperature,
            top_p=self._rollout_cfg.top_p,
            max_tokens=self._rollout_cfg.max_response_tokens,
        )

        loop = asyncio.get_event_loop()
        outputs = await loop.run_in_executor(
            None,
            lambda: self._vllm.generate(
                [prompt_text],
                sampling_params,
                lora_request=self._lora_request,
            ),
        )

        if outputs and outputs[0].outputs:
            return outputs[0].outputs[0].text
        return ""

    def _parse_tool_call(self, response: str) -> dict | None:
        """Extract tool call JSON from <tool_call>...</tool_call> tags."""
        match = _TOOL_CALL_PATTERN.search(response)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    def _detect_loop(self, recent_actions: list[str]) -> bool:
        """Detect if last N actions are identical (stuck in a loop)."""
        if len(recent_actions) < _LOOP_DETECTION_WINDOW:
            return False
        window = recent_actions[-_LOOP_DETECTION_WINDOW:]
        return len(set(window)) == 1
