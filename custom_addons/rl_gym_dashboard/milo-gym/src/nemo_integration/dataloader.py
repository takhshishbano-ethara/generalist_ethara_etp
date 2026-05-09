"""MiloCurriculumDataloader — NeMo-RL StatefulDataLoader wrapping ScalingInterRLSampler.

Replaces NeMo-RL's default data loading. Each iteration yields a BatchedDataDict
with prompts drawn from curriculum-selected tasks.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import torch

from src.core.schemas import TaskSpec
from src.training.curriculum import ScalingInterRLSampler

log = logging.getLogger(__name__)

STOP_STRINGS = ["</tool_call>"]

SYSTEM_PROMPT = """You are a coding agent. You have access to tools for interacting with a code repository in a Docker container.

Available tools:
- apply_patch: Apply a unified diff patch to files
- run_tests: Run the test suite
- read_file: Read file contents
- list_files: List directory contents
- search: Search for patterns in code
- submit: Submit your final patch

Respond with a tool call in this format:
<tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>

Your goal: Fix the described issue by producing a correct patch that passes all tests."""


class MiloCurriculumDataloader:
    """Curriculum-driven data source for NeMo-RL's GRPO training loop.

    Wraps ScalingInterRLSampler to produce batches of prompts from
    difficulty-appropriate tasks based on current training progress.

    Conforms to NeMo-RL's dataloader interface:
        __iter__() -> yields dict batches
        state_dict() / load_state_dict() for checkpointing
    """

    def __init__(
        self,
        sampler: ScalingInterRLSampler,
        task_registry: dict[int, TaskSpec],
        tokenizer: Any,
        num_prompts_per_step: int,
        system_prompt: str | None = None,
    ) -> None:
        self._sampler = sampler
        self._task_registry = task_registry
        self._tokenizer = tokenizer
        self._num_prompts = num_prompts_per_step
        self._system_prompt = system_prompt or SYSTEM_PROMPT
        self._epoch = 0

    @property
    def max_turns(self) -> int:
        return self._sampler.max_turns

    @property
    def current_phase(self) -> int:
        return self._sampler.current_phase

    def __len__(self) -> int:
        return len(self._task_registry) // self._num_prompts

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            batch = self._sample_one_batch()
            yield batch

    def set_epoch(self, epoch: int) -> None:
        self._epoch = epoch

    def _sample_one_batch(self) -> dict[str, Any]:
        task_ids = self._sampler.sample_batch(self._num_prompts)

        message_logs: list[list[dict[str, str]]] = []
        extra_env_infos: list[dict[str, Any]] = []
        task_names: list[str] = []

        for task_id in task_ids:
            task = self._task_registry.get(task_id)
            if task is None:
                log.warning("Task %d not found in registry, skipping", task_id)
                continue

            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": self._build_user_prompt(task)},
            ]
            message_logs.append(messages)

            extra_env_infos.append({
                "task_id": task.task_id,
                "instance_id": task.instance_id,
                "repo": task.repo,
                "docker_image": task.docker_image,
                "test_patch": task.test_patch,
                "evaluation_script": task.evaluation_script,
                "max_turns": self._sampler.max_turns,
                "timeout_seconds": task.timeout_seconds,
                "step_rewards": [],
                "turn_spans": [],
                "turn_count": 0,
            })

            task_names.append("milo_docker")

        batch_size = len(message_logs)

        # Compute token lengths for NeMo-RL's batching logic
        lengths: list[int] = []
        for msgs in message_logs:
            text = self._tokenizer.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True
            )
            tokens = self._tokenizer(text, return_tensors="pt", add_special_tokens=False)
            lengths.append(tokens["input_ids"].shape[1])

        # NeMo-RL expected format (matches rl_collate_fn output)
        return {
            "message_log": message_logs,
            "length": torch.tensor(lengths, dtype=torch.long),
            "loss_multiplier": torch.ones(batch_size, dtype=torch.float32),
            "extra_env_info": extra_env_infos,
            "task_name": task_names,
            "idx": torch.arange(batch_size, dtype=torch.long),
            "batch_max_length": max(lengths) if lengths else 1,
            "stop_strings": [STOP_STRINGS for _ in range(batch_size)],
        }

    def _build_user_prompt(self, task: TaskSpec) -> str:
        return (
            f"## Repository: {task.repo}\n\n"
            f"## Problem\n{task.problem_statement}\n\n"
            f"## Instructions\n"
            f"Fix this issue by making the necessary code changes. "
            f"Use the available tools to explore the repository, make changes, "
            f"run tests, and submit your patch when ready."
        )

    def update_metrics(self, success_rate: float, reward_variance: float) -> None:
        self._sampler.update(success_rate, reward_variance)

    def state_dict(self) -> dict[str, Any]:
        return {
            "sampler_state": self._sampler.get_state_dict(),
        }

    def load_state_dict(self, state: dict[str, Any]) -> None:
        sampler_state = state.get("sampler_state")
        if sampler_state:
            self._sampler.load_state_dict(sampler_state)
