"""Custom reward manager that grades trajectories via Docker execution."""

from __future__ import annotations

import asyncio
import logging

import torch

from src.core.schemas import RewardResult, TaskSpec, Trajectory
from src.rollout.docker_executor import DockerExecutor, DockerResult
from src.rollout.patch_utils import extract_patch, is_compact_filtered

log = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from sync context, handling nested event loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


class RewardOutput(dict):
    """Dict-like return from reward manager that also behaves as a tensor for backward compat."""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            rewards = self["rewards"]
            return getattr(rewards, name)

    def __eq__(self, other):
        return self["rewards"] == other

    def __len__(self):
        return len(self["rewards"])

    def __iter__(self):
        return iter(self["rewards"])


def milo_compute_score(
    data_source: str,
    solution_str: str,
    ground_truth: str,
    extra_info: dict,
) -> float:
    """verl-compatible reward function signature. Returns 0.0 or 1.0."""
    docker_result = extra_info.get("docker_result")
    if docker_result is None:
        return 0.0
    if isinstance(docker_result, DockerResult):
        if docker_result.f2p_pass and docker_result.p2p_pass:
            return 1.0
        return 0.0
    if isinstance(docker_result, dict):
        f2p_pass = docker_result.get("f2p_pass", False)
        p2p_pass = docker_result.get("p2p_pass", True)
        return 1.0 if (f2p_pass and p2p_pass) else 0.0
    return 0.0


class MiloRewardManager:
    """Grades trajectories via Docker execution with compact filtering."""

    def __init__(
        self,
        tokenizer,
        executor: DockerExecutor | None = None,
        task_registry: dict[str, TaskSpec] | None = None,
        compact_filtering: bool = True,
        max_resp_len: int = 32768,
    ):
        self._tokenizer = tokenizer
        self._executor = executor
        self._task_registry = task_registry or {}
        self._compact_filtering = compact_filtering
        self._max_resp_len = max_resp_len
        self._prm_scorer = None
        self._shaper = None
        self._prm_config = None

    def configure_prm(self, prm_config) -> None:
        """Attach PRM scorer and shaper. Safe to call multiple times."""
        from src.core.config import PRMConfig
        from src.prm.scorer import LLMJudgeScorer, TrainedPRMScorer
        from src.prm.shaper import PotentialShaper

        if not prm_config.enabled:
            self._prm_scorer = None
            self._shaper = None
            self._prm_config = None
            return

        self._prm_config = prm_config
        self._shaper = PotentialShaper.from_config(prm_config)

        if prm_config.mode == "bedrock":
            from src.prm.bedrock_scorer import BedrockClaudeScorer
            self._prm_scorer = BedrockClaudeScorer(prm_config)
        elif prm_config.mode == "llm_judge":
            self._prm_scorer = LLMJudgeScorer(prm_config)
        elif prm_config.mode in ("trained", "self_prm"):
            self._prm_scorer = TrainedPRMScorer(prm_config)
        else:
            log.warning("Unknown PRM mode: %s, disabling", prm_config.mode)
            self._prm_scorer = None
            self._shaper = None

    def __call__(self, data: dict) -> dict[str, torch.Tensor]:
        """Compute rewards for a batch.

        data keys: 'responses' (list[str]), 'task_ids' (list[str]),
        'episode_lengths' (list[int]), 'hit_max_turns' (list[bool]),
        'timed_out' (list[bool])

        Returns dict with keys:
            'rewards': binary reward tensor (0/1)
            'shaped_returns': PRM-shaped scalar per trajectory (equals rewards when PRM disabled)
            'masks': compact filter mask
        """
        responses: list[str] = data["responses"]
        task_ids: list[str] = data["task_ids"]
        batch_size = len(responses)

        patches = self._extract_patches(responses)
        results = self._grade_batch(patches, task_ids)
        mask = self._build_mask(data, patches)

        rewards = torch.zeros(batch_size, dtype=torch.float32)
        for i, result in enumerate(results):
            rewards[i] = self._compute_binary_reward(result)

        if self._compact_filtering:
            rewards = rewards * mask

        shaped_returns = rewards.clone()

        if self._prm_scorer is not None and self._shaper is not None:
            trajectories: list[Trajectory] = data.get("trajectories", [])
            task_descriptions: list[str] = data.get("task_descriptions", [""] * batch_size)
            if trajectories:
                step_rewards_batch = self._score_with_prm(
                    trajectories, task_descriptions, rewards
                )
                for idx, (traj, step_rws) in enumerate(zip(trajectories, step_rewards_batch)):
                    traj.step_rewards = step_rws
                    sr = self._shaper.compute_shaped_return(step_rws)
                    traj.shaped_return = sr
                    shaped_returns[idx] = sr

        return RewardOutput({
            "rewards": rewards,
            "shaped_returns": shaped_returns,
            "masks": mask,
        })

    def _extract_patches(self, responses: list[str]) -> list[str]:
        return [extract_patch(resp) for resp in responses]

    def _grade_batch(
        self, patches: list[str], task_ids: list[str]
    ) -> list[RewardResult]:
        if self._executor is None:
            return [
                RewardResult(reward=0.0, mask=False, error="No executor available")
                for _ in patches
            ]

        items: list[tuple[TaskSpec, str]] = []
        index_map: list[int] = []

        for i, (patch, task_id) in enumerate(zip(patches, task_ids)):
            if not patch.strip():
                continue
            task = self._task_registry.get(task_id)
            if task is None:
                continue
            items.append((task, patch))
            index_map.append(i)

        results: list[RewardResult] = [
            RewardResult(reward=0.0, mask=False, error="Empty patch or missing task")
            for _ in patches
        ]

        if not items:
            return results

        try:
            docker_results = _run_async(self._executor.run_batch(items))
        except Exception as e:
            log.exception("Batch grading failed")
            return results

        for batch_idx, docker_result in zip(index_map, docker_results):
            reward = self._compute_binary_reward(docker_result)
            results[batch_idx] = RewardResult(
                reward=reward,
                mask=True,
                f2p_passed=docker_result.f2p_passed,
                f2p_total=docker_result.f2p_total,
                p2p_passed=docker_result.p2p_passed,
                p2p_total=docker_result.p2p_total,
                timed_out=docker_result.timed_out,
                error=docker_result.error,
            )

        return results

    def _build_mask(self, data: dict, patches: list[str]) -> torch.Tensor:
        batch_size = len(patches)
        mask = torch.ones(batch_size, dtype=torch.float32)

        hit_max_turns: list[bool] = data.get("hit_max_turns", [False] * batch_size)
        timed_out: list[bool] = data.get("timed_out", [False] * batch_size)

        for i in range(batch_size):
            if hit_max_turns[i]:
                mask[i] = 0.0
            elif timed_out[i]:
                mask[i] = 0.0
            elif not patches[i].strip():
                mask[i] = 0.0

        return mask

    def _compute_binary_reward(self, result: DockerResult | RewardResult) -> float:
        if isinstance(result, DockerResult):
            return 1.0 if (result.f2p_pass and result.p2p_pass) else 0.0
        return 1.0 if (result.f2p_pass and result.p2p_pass) else 0.0

    def _score_with_prm(
        self,
        trajectories: list[Trajectory],
        task_descriptions: list[str],
        outcome_rewards: torch.Tensor,
    ) -> list[list[float]]:
        coro = self._score_with_prm_async(trajectories, task_descriptions, outcome_rewards)
        try:
            return _run_async(coro)
        except Exception as e:
            log.error("PRM scoring failed: %s", e)
            return [[] for _ in trajectories]

    async def _score_with_prm_async(
        self,
        trajectories: list[Trajectory],
        task_descriptions: list[str],
        outcome_rewards: torch.Tensor,
    ) -> list[list[float]]:
        valid_indices: list[int] = []
        valid_turns: list[list] = []
        valid_descs: list[str] = []

        for i, traj in enumerate(trajectories):
            if traj.turns:
                valid_indices.append(i)
                valid_turns.append(traj.turns)
                valid_descs.append(
                    task_descriptions[i] if i < len(task_descriptions) else ""
                )

        all_step_rewards: list[list[float]] = [[] for _ in trajectories]

        if not valid_turns:
            return all_step_rewards

        batch_scores = await self._prm_scorer.score_trajectory_batch(
            valid_turns, valid_descs
        )

        for batch_idx, orig_idx in enumerate(valid_indices):
            traj = trajectories[orig_idx]
            prm_scores = batch_scores[batch_idx]
            outcome = outcome_rewards[orig_idx].item()

            shaped = self._shaper.shape(prm_scores, outcome)
            all_step_rewards[orig_idx] = shaped

            for turn, score in zip(traj.turns, prm_scores):
                turn.prm_score = score

        return all_step_rewards
