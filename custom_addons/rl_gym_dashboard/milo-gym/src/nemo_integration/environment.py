"""MiloDockerEnvironment — NeMo-RL EnvironmentInterface wrapping DockerSandboxTool.

Implements multi-turn Docker sandbox interaction as a Ray actor.
Each sample in the batch corresponds to one SWE-bench instance with
a persistent Docker container across all turns of the episode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple, TypedDict

import torch

log = logging.getLogger(__name__)

TOOL_CALL_PATTERN = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
STOP_STRINGS = ["</tool_call>"]


class MiloEpisodeMetadata(TypedDict):
    task_id: str
    instance_id: str
    repo: str
    docker_image: str
    test_patch: str
    evaluation_script: str
    max_turns: int
    timeout_seconds: int
    step_rewards: list[float]
    turn_spans: list[dict[str, int]]
    turn_count: int
    container_id: str | None
    episode_start_time: float
    submitted: bool
    patch: str


class EnvironmentReturn(NamedTuple):
    observations: list[dict[str, str]]
    metadata: list[Any]
    next_stop_strings: list[list[str] | None]
    rewards: torch.Tensor
    terminateds: torch.Tensor
    answers: list[str | None] | None


LLMMessageLogType = list[dict[str, Any]]


class MiloDockerEnvironment:
    """NeMo-RL environment wrapping DockerSandboxTool for multi-turn code generation.

    Lifecycle:
        1. First step() creates Docker container from ECR image
        2. Subsequent step() calls execute tool actions in the container
        3. global_post_process_and_metrics() finalizes, grades, PRM-scores, and cleans up

    Must be decorated with @ray.remote when used with NeMo-RL.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        from src.core.config import GatedRewardConfig, PRMConfig
        from src.rollout.docker_executor import DockerExecutor
        from src.rollout.docker_tool import DockerSandboxTool
        from src.training.gated_rewards import GatedRewardComputer

        self._config = config
        self._executor = DockerExecutor(
            max_concurrent=config.get("max_concurrent_containers", 64),
            timeout=config.get("timeout_seconds", 1800),
        )
        self._tool = DockerSandboxTool(self._executor)

        gated_config = GatedRewardConfig(**config.get("gated_reward_config", {}))
        self._gated_computer = GatedRewardComputer(gated_config)

        self._prm_config = PRMConfig(**config.get("prm_config", {}))
        self._scorer = self._init_scorer()
        self._thread_pool = ThreadPoolExecutor(max_workers=4)

    def _init_scorer(self):
        """Initialize PRM scorer based on config. Returns None if disabled."""
        prm_mode = self._config.get("prm_mode", "bedrock")
        if prm_mode == "none" or prm_mode == "disabled":
            return None

        try:
            if prm_mode == "bedrock":
                from src.prm.bedrock_scorer import BedrockClaudeScorer
                return BedrockClaudeScorer(
                    region=self._config.get("embedding_region", "us-east-1"),
                    model_id=self._config.get("prm_model_id", "anthropic.claude-3-sonnet-20240229-v1:0"),
                )
            elif prm_mode == "local":
                from src.prm.scorer import TrainedPRMScorer
                return TrainedPRMScorer(
                    model_path=self._config.get("prm_model_path", ""),
                )
        except Exception as e:
            log.warning("Failed to initialize PRM scorer (%s): %s", prm_mode, e)
            return None

    def step(
        self,
        message_log_batch: list[LLMMessageLogType],
        metadata_batch: list[MiloEpisodeMetadata],
    ) -> EnvironmentReturn:
        batch_size = len(message_log_batch)

        observations: list[dict[str, str]] = []
        new_metadata: list[MiloEpisodeMetadata] = []
        rewards_list: list[float] = []
        terminateds_list: list[bool] = []

        for i in range(batch_size):
            messages = message_log_batch[i]
            meta = metadata_batch[i]

            obs, reward, done, updated_meta = self._step_single(messages, meta)
            observations.append(obs)
            new_metadata.append(updated_meta)
            rewards_list.append(reward)
            terminateds_list.append(done)

        return EnvironmentReturn(
            observations=observations,
            metadata=new_metadata,
            next_stop_strings=[STOP_STRINGS if not t else None for t in terminateds_list],
            rewards=torch.tensor(rewards_list, dtype=torch.float32),
            terminateds=torch.tensor(terminateds_list, dtype=torch.bool),
            answers=None,
        )

    def _step_single(
        self,
        messages: LLMMessageLogType,
        meta: MiloEpisodeMetadata,
    ) -> tuple[dict[str, str], float, bool, MiloEpisodeMetadata]:
        updated_meta = dict(meta)  # shallow copy

        # Initialize container on first turn
        if meta.get("container_id") is None:
            container_id = self._create_container(meta)
            updated_meta["container_id"] = container_id
            updated_meta["episode_start_time"] = time.time()
            updated_meta["submitted"] = False
            updated_meta["patch"] = ""

        # Parse tool call from last assistant message
        last_msg = messages[-1] if messages else {}
        content = last_msg.get("content", "")

        action, arguments = self._parse_tool_call(content)

        # Check timeout
        elapsed = time.time() - updated_meta.get("episode_start_time", time.time())
        if elapsed > meta["timeout_seconds"]:
            return (
                {"role": "user", "content": "Episode timed out."},
                0.0,
                True,
                updated_meta,
            )

        # Check max turns
        updated_meta["turn_count"] = meta.get("turn_count", 0) + 1
        if updated_meta["turn_count"] >= meta["max_turns"]:
            return (
                {"role": "user", "content": "Maximum turns reached."},
                0.0,
                True,
                updated_meta,
            )

        # Execute action
        if action == "submit":
            updated_meta["submitted"] = True
            patch = arguments.get("patch", arguments.get("content", ""))
            updated_meta["patch"] = patch
            return (
                {"role": "user", "content": "Patch submitted. Episode complete."},
                0.0,
                True,
                updated_meta,
            )

        if action is None:
            return (
                {"role": "user", "content": "No valid tool call found. Use <tool_call>{...}</tool_call> format."},
                0.0,
                False,
                updated_meta,
            )

        # Execute tool in Docker container
        try:
            result = self._execute_tool(updated_meta["container_id"], action, arguments)
            observation = {"role": "user", "content": f"Tool result ({action}):\n{result}"}
        except Exception as e:
            observation = {"role": "user", "content": f"Tool error ({action}): {e}"}

        return observation, 0.0, False, updated_meta

    def _parse_tool_call(self, content: str) -> tuple[str | None, dict[str, Any]]:
        match = TOOL_CALL_PATTERN.search(content)
        if not match:
            return None, {}

        try:
            call_data = json.loads(match.group(1))
            action = call_data.get("name", call_data.get("tool", None))
            arguments = call_data.get("arguments", call_data.get("args", {}))
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            return action, arguments
        except (json.JSONDecodeError, TypeError):
            return None, {}

    def _create_container(self, meta: MiloEpisodeMetadata) -> str:
        loop = asyncio.new_event_loop()
        try:
            container_id = loop.run_until_complete(
                self._executor.create_container(
                    image=meta.get("docker_image", ""),
                    instance_id=meta["instance_id"],
                )
            )
            return container_id
        finally:
            loop.close()

    def _execute_tool(self, container_id: str, action: str, arguments: dict) -> str:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._tool.execute(
                    instance_id=container_id,
                    parameters={"action": action, **arguments},
                )
            )
            if isinstance(result, tuple):
                return str(result[0])
            return str(result)
        finally:
            loop.close()

    def global_post_process_and_metrics(
        self,
        batch: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, float]]:
        """Post-rollout processing: grade, PRM-score, partial credit, format penalties, cleanup.

        This runs AFTER all multi-turn episodes complete. It:
        1. Grades submitted patches via Docker (f2p/p2p evaluation)
        2. Runs PRM scoring on trajectories
        3. Computes partial credit for failed trajectories (GTPO paper §3.4)
        4. Computes format penalties per turn (GTPO paper §3.3)
        5. Computes gated rewards via GatedRewardComputer (with partial credit + format)
        6. Injects step_rewards + turn_spans into batch for advantage computation
        7. Cleans up Docker containers
        """
        import copy

        from src.prm.shaper import PotentialShaper
        from src.training.format_penalty import FormatPenaltyComputer, FormatPenaltyConfig
        from src.training.gated_rewards import OutcomeType, TrajectoryRewardInput
        from src.training.partial_credit import PartialCreditComputer, PartialCreditConfig

        extra_env_infos = batch.get("extra_env_info", [])
        batch_size = len(extra_env_infos)

        if batch_size == 0:
            return batch, {}

        # Deep-copy extra_env_info to break shared references from repeat_interleave.
        # Without this, all N generations from the same prompt share one dict,
        # and writing step_rewards to one overwrites all N.
        extra_env_infos = [copy.copy(d) if isinstance(d, dict) else d for d in extra_env_infos]
        batch["extra_env_info"] = extra_env_infos

        # Grade all submitted patches
        rewards, outcomes = self._grade_batch(extra_env_infos)

        # PRM scoring (if enabled)
        step_rewards_batch = self._score_prm_batch(batch, extra_env_infos)

        # Partial credit for failed trajectories
        partial_credits = self._compute_partial_credits(
            batch, extra_env_infos, outcomes
        )

        # Format penalties per turn
        format_penalties = self._compute_format_penalties(batch, extra_env_infos)

        # Build TrajectoryRewardInputs for gated reward computation
        gated_inputs: list[TrajectoryRewardInput] = []
        for i in range(batch_size):
            meta = extra_env_infos[i]
            gated_inputs.append(TrajectoryRewardInput(
                outcome=outcomes[i],
                step_rewards=step_rewards_batch[i],
                episode_length=meta.get("turn_count", 0),
                max_turns=meta.get("max_turns", 50),
            ))

        # Compute gated per-turn rewards with partial credit + format penalties
        per_turn_rewards = self._gated_computer.compute_per_turn_rewards(
            gated_inputs,
            partial_credits=partial_credits,
            format_penalties=format_penalties,
        )

        # Compute total scalar rewards (for GRPO baseline)
        total_rewards = self._gated_computer.compute_gated_rewards(gated_inputs)
        existing_reward = batch.get("total_reward")
        if existing_reward is not None and hasattr(existing_reward, "device"):
            batch["total_reward"] = total_rewards.to(existing_reward.device)
        else:
            batch["total_reward"] = total_rewards

        for i in range(batch_size):
            extra_env_infos[i]["step_rewards"] = per_turn_rewards[i]
            extra_env_infos[i]["turn_count"] = len(per_turn_rewards[i])
            # turn_spans (token-level boundaries) computed lazily by MiloAdvantageEstimator
            # because token_mask is not yet available at this post-rollout stage

        self._cleanup_containers(extra_env_infos)

        # Metrics
        num_pass = sum(1 for o in outcomes if o == OutcomeType.PASS)
        num_submit = sum(1 for m in extra_env_infos if m.get("submitted", False))
        avg_turns = sum(m.get("turn_count", 0) for m in extra_env_infos) / max(batch_size, 1)
        avg_partial_credit = sum(partial_credits) / max(batch_size, 1) if partial_credits else 0.0
        num_format_violations = sum(
            1 for fp in format_penalties for p in fp if p < 0
        ) if format_penalties else 0

        metrics = {
            "env/success_rate": num_pass / max(batch_size, 1),
            "env/submit_rate": num_submit / max(batch_size, 1),
            "env/avg_turns": avg_turns,
            "env/num_pass": float(num_pass),
            "env/batch_size": float(batch_size),
            "env/avg_partial_credit": avg_partial_credit,
            "env/format_violations": float(num_format_violations),
        }

        return batch, metrics

    def _compute_partial_credits(
        self,
        batch: dict[str, Any],
        env_infos: list[MiloEpisodeMetadata],
        outcomes: list[Any],
    ) -> list[float]:
        """Compute partial credit for failed trajectories via code similarity."""
        from src.training.gated_rewards import OutcomeType
        from src.training.partial_credit import PartialCreditComputer, PartialCreditConfig

        config = PartialCreditConfig(
            enabled=self._config.get("partial_credit_enabled", True),
            alpha=self._config.get("partial_credit_alpha", 0.5),
            use_embeddings=self._config.get("partial_credit_use_embeddings", True),
            embedding_region=self._config.get("embedding_region", "us-east-1"),
        )
        computer = PartialCreditComputer(config=config)

        message_logs = batch.get("message_log", [])
        all_codes: list[str] = []
        all_outcomes: list[bool] = []

        for i, meta in enumerate(env_infos):
            messages = message_logs[i] if i < len(message_logs) else []
            code = computer.extract_code_from_trajectory(
                messages if isinstance(messages, list) else []
            )
            all_codes.append(code)
            all_outcomes.append(outcomes[i] == OutcomeType.PASS)

        group_size = self._config.get("num_generations_per_prompt", 8)
        return computer.compute_batch_partial_credit(
            all_codes, all_outcomes, group_size
        )

    def _compute_format_penalties(
        self,
        batch: dict[str, Any],
        env_infos: list[MiloEpisodeMetadata],
    ) -> list[list[float]]:
        """Compute format penalties for each trajectory's turns."""
        from src.training.format_penalty import FormatPenaltyComputer, FormatPenaltyConfig

        config = FormatPenaltyConfig(
            enabled=self._config.get("format_penalty_enabled", True),
            penalty_per_violation=self._config.get("format_penalty_value", -0.1),
            first_turn_must_have_tool=self._config.get("format_first_turn_tool_required", True),
        )
        computer = FormatPenaltyComputer(config)

        message_logs = batch.get("message_log", [])
        all_penalties: list[list[float]] = []

        for i, meta in enumerate(env_infos):
            messages = message_logs[i] if i < len(message_logs) else []
            if isinstance(messages, list):
                penalties = computer.compute_trajectory_penalties(messages)
            else:
                penalties = [0.0] * meta.get("turn_count", 0)
            all_penalties.append(penalties)

        return all_penalties

    def _grade_batch(
        self,
        env_infos: list[MiloEpisodeMetadata],
    ) -> tuple[list[float], list[Any]]:
        from src.training.gated_rewards import OutcomeType

        rewards: list[float] = []
        outcomes: list[OutcomeType] = []

        for meta in env_infos:
            if not meta.get("submitted", False) or not meta.get("patch"):
                if meta.get("turn_count", 0) >= meta.get("max_turns", 50):
                    outcomes.append(OutcomeType.TIMEOUT)
                else:
                    outcomes.append(OutcomeType.EMPTY)
                rewards.append(0.0)
                continue

            # Run Docker evaluation
            try:
                result = self._run_evaluation(meta)
                if result.get("f2p_pass") and result.get("p2p_pass"):
                    outcomes.append(OutcomeType.PASS)
                    rewards.append(1.0)
                else:
                    outcomes.append(OutcomeType.FAIL)
                    rewards.append(0.0)
            except Exception as e:
                log.warning("Grading failed for %s: %s", meta.get("task_id"), e)
                outcomes.append(OutcomeType.FAIL)
                rewards.append(0.0)

        return rewards, outcomes

    def _run_evaluation(self, meta: MiloEpisodeMetadata) -> dict[str, bool]:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._executor.evaluate(
                    container_id=meta.get("container_id", ""),
                    patch=meta.get("patch", ""),
                    test_patch=meta.get("test_patch", ""),
                    evaluation_script=meta.get("evaluation_script", ""),
                )
            )
            return result
        finally:
            loop.close()

    def _score_prm_batch(
        self,
        batch: dict[str, Any],
        env_infos: list[MiloEpisodeMetadata],
    ) -> list[list[float]]:
        """Score turns with PRM. Returns per-turn shaped rewards."""
        from src.prm.shaper import PotentialShaper

        shaper = PotentialShaper(
            alpha=self._prm_config.shaping_alpha,
            gamma=self._prm_config.gtpo_gamma,
        )

        batch_size = len(env_infos)
        step_rewards_batch: list[list[float]] = []

        message_logs = batch.get("message_log", [])

        for i in range(batch_size):
            meta = env_infos[i]
            turn_count = meta.get("turn_count", 0)

            if turn_count == 0:
                step_rewards_batch.append([])
                continue

            # For now, use uniform PRM scores (actual PRM scoring is expensive)
            # In production, this calls BedrockClaudeScorer or TrainedPRMScorer
            prm_scores: list[float | None] = [None] * turn_count

            if self._scorer is not None and i < len(message_logs):
                try:
                    prm_scores = self._score_trajectory_prm(message_logs[i])
                except Exception as e:
                    log.debug("PRM scoring failed for sample %d: %s", i, e)

            # Shape with PBRS (do NOT pass outcome_reward here — 
            # GatedRewardComputer.compute_per_turn_rewards adds it separately)
            shaped = shaper.shape(prm_scores, outcome_reward=0.0)
            step_rewards_batch.append(shaped)

        return step_rewards_batch

    def _score_trajectory_prm(self, messages: list[dict[str, Any]]) -> list[float | None]:
        """Score each assistant turn with PRM."""
        assistant_turns = [m for m in messages if m.get("role") == "assistant"]
        if not assistant_turns or self._scorer is None:
            return [None] * len(assistant_turns)

        scores: list[float | None] = []
        for turn in assistant_turns:
            try:
                score = self._scorer.score_turn(turn.get("content", ""))
                scores.append(float(score) if score is not None else None)
            except Exception:
                scores.append(None)
        return scores

    def _cleanup_containers(self, env_infos: list[MiloEpisodeMetadata]) -> None:
        loop = asyncio.new_event_loop()
        try:
            tasks = []
            for meta in env_infos:
                container_id = meta.get("container_id")
                if container_id:
                    tasks.append(self._executor.destroy_container(container_id))
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            loop.close()
